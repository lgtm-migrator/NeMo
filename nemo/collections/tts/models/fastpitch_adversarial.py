# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import contextlib
from dataclasses import dataclass
from itertools import chain
from typing import Any, Dict, Optional

import numpy as np
import torch
from hydra.utils import instantiate
from omegaconf import MISSING, DictConfig, OmegaConf
from pytorch_lightning import Trainer
from pytorch_lightning.loggers import LoggerCollection, TensorBoardLogger, WandbLogger
import wandb

from nemo.collections.common.parts.preprocessing import parsers
from nemo.collections.tts.helpers.helpers import plot_alignment_to_numpy, plot_spectrogram_to_numpy, fastpitch_log_to_wandb_func
from nemo.collections.tts.losses.aligner_loss import BinLoss, ForwardSumLoss
from nemo.collections.tts.losses.fastpitchloss import DurationLoss, MelLoss, PitchLoss
from nemo.collections.tts.models.base import SpectrogramGenerator
from nemo.collections.tts.modules.fastpitch import FastPitchModule
from nemo.collections.tts.torch.tts_data_types import SpeakerID
from nemo.core.classes import Exportable
from nemo.core.classes.common import PretrainedModelInfo, typecheck
from nemo.core.neural_types.elements import (
    Index,
    LengthsType,
    MelSpectrogramType,
    ProbsType,
    RegressionValuesType,
    TokenDurationType,
    TokenIndex,
    TokenLogDurationType,
)
from nemo.core.neural_types.neural_type import NeuralType
from nemo.core.optim.lr_scheduler import NoamAnnealing
from nemo.utils import logging, model_utils


class FastPitchAdversarialModel(SpectrogramGenerator, Exportable):

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        # Convert to Hydra 1.0 compatible DictConfig
        cfg = model_utils.convert_model_config_to_dict_config(cfg)
        cfg = model_utils.maybe_update_config_version(cfg)

        # Setup normalizer
        self.normalizer = None
        self.text_normalizer_call = None
        self.text_normalizer_call_kwargs = {}
        self._setup_normalizer(cfg)

        self.learn_alignment = cfg.get("learn_alignment", False)

        # Setup vocabulary (=tokenizer) and input_fft_kwargs (supported only with self.learn_alignment=True)
        input_fft_kwargs = {}
        if self.learn_alignment:
            self.vocab = None
            self.ds_class_name = cfg.train_ds.dataset._target_.split(".")[-1]

            if self.ds_class_name == "TTSDataset":
                self._setup_tokenizer(cfg)
                assert self.vocab is not None
                input_fft_kwargs["n_embed"] = len(self.vocab.tokens)
                input_fft_kwargs["padding_idx"] = self.vocab.pad
            elif self.ds_class_name == "AudioToCharWithPriorAndPitchDataset":
                logging.warning(
                    "AudioToCharWithPriorAndPitchDataset class has been deprecated. No support for"
                    " training or finetuning. Only inference is supported."
                )
                tokenizer_conf = self._get_default_text_tokenizer_conf()
                self._setup_tokenizer(tokenizer_conf)
                assert self.vocab is not None
                input_fft_kwargs["n_embed"] = len(self.vocab.tokens)
                input_fft_kwargs["padding_idx"] = self.vocab.pad
            else:
                raise ValueError(f"Unknown dataset class: {self.ds_class_name}")

        self._parser = None
        self._tb_logger = None
        super().__init__(cfg=cfg, trainer=trainer)

        self.bin_loss_warmup_epochs = cfg.get("bin_loss_warmup_epochs", 100)
        self.log_train_images = False

        loss_scale = 0.1 if self.learn_alignment else 1.0
        dur_loss_scale = loss_scale
        pitch_loss_scale = loss_scale
        if "dur_loss_scale" in cfg:
            dur_loss_scale = cfg.dur_loss_scale
        if "pitch_loss_scale" in cfg:
            pitch_loss_scale = cfg.pitch_loss_scale

        self.mel_loss = MelLoss()
        self.pitch_loss = PitchLoss(loss_scale=pitch_loss_scale)
        self.duration_loss = DurationLoss(loss_scale=dur_loss_scale)

        self.aligner = None
        if self.learn_alignment:
            self.aligner = instantiate(self._cfg.alignment_module)
            self.forward_sum_loss = ForwardSumLoss()
            self.bin_loss = BinLoss()

        self.preprocessor = instantiate(self._cfg.preprocessor)
        input_fft = instantiate(self._cfg.input_fft, **input_fft_kwargs)
        output_fft = instantiate(self._cfg.output_fft)
        duration_predictor = instantiate(self._cfg.duration_predictor)
        pitch_predictor = instantiate(self._cfg.pitch_predictor)
        self.discriminator = instantiate(self._cfg.discriminator)
        self.splice_length = self._cfg.splice_length

        self.fastpitch = FastPitchModule(
            input_fft,
            output_fft,
            duration_predictor,
            pitch_predictor,
            self.aligner,
            cfg.n_speakers,
            cfg.symbols_embedding_dim,
            cfg.pitch_embedding_kernel_size,
            cfg.n_mel_channels,
        )
        self._input_types = self._output_types = None

    def _get_default_text_tokenizer_conf(self):
        text_tokenizer: TextTokenizerConfig = TextTokenizerConfig()
        return OmegaConf.create(OmegaConf.to_yaml(text_tokenizer))

    def _setup_normalizer(self, cfg):
        if "text_normalizer" in cfg:
            normalizer_kwargs = {}

            if "whitelist" in cfg.text_normalizer:
                normalizer_kwargs["whitelist"] = self.register_artifact(
                    'text_normalizer.whitelist', cfg.text_normalizer.whitelist
                )

            self.normalizer = instantiate(cfg.text_normalizer, **normalizer_kwargs)
            self.text_normalizer_call = self.normalizer.normalize
            if "text_normalizer_call_kwargs" in cfg:
                self.text_normalizer_call_kwargs = cfg.text_normalizer_call_kwargs

    def _setup_tokenizer(self, cfg):
        text_tokenizer_kwargs = {}
        if "g2p" in cfg.text_tokenizer:
            g2p_kwargs = {}

            if "phoneme_dict" in cfg.text_tokenizer.g2p:
                g2p_kwargs["phoneme_dict"] = self.register_artifact(
                    'text_tokenizer.g2p.phoneme_dict', cfg.text_tokenizer.g2p.phoneme_dict,
                )

            if "heteronyms" in cfg.text_tokenizer.g2p:
                g2p_kwargs["heteronyms"] = self.register_artifact(
                    'text_tokenizer.g2p.heteronyms', cfg.text_tokenizer.g2p.heteronyms,
                )

            text_tokenizer_kwargs["g2p"] = instantiate(cfg.text_tokenizer.g2p, **g2p_kwargs)

        self.vocab = instantiate(cfg.text_tokenizer, **text_tokenizer_kwargs)

    @property
    def tb_logger(self):
        if self._tb_logger is None:
            if self.logger is None and self.logger.experiment is None:
                return None
            tb_logger = self.logger.experiment
            if isinstance(self.logger, LoggerCollection):
                for logger in self.logger:
                    if isinstance(logger, TensorBoardLogger):
                        tb_logger = logger.experiment
                        break
            self._tb_logger = tb_logger
        return self._tb_logger

    @property
    def parser(self):
        if self._parser is not None:
            return self._parser

        if self.learn_alignment:
            ds_class_name = self._cfg.train_ds.dataset._target_.split(".")[-1]

            if ds_class_name == "TTSDataset":
                self._parser = self.vocab.encode
            elif ds_class_name == "AudioToCharWithPriorAndPitchDataset":
                if self.vocab is None:
                    tokenizer_conf = self._get_default_text_tokenizer_conf()
                    self._setup_tokenizer(tokenizer_conf)
                self._parser = self.vocab.encode
            else:
                raise ValueError(f"Unknown dataset class: {ds_class_name}")
        else:
            self._parser = parsers.make_parser(
                labels=self._cfg.labels,
                name='en',
                unk_id=-1,
                blank_id=-1,
                do_normalize=True,
                abbreviation_version="fastpitch",
                make_table=False,
            )
        return self._parser

    def parse(self, str_input: str, normalize=True) -> torch.tensor:
        if self.training:
            logging.warning("parse() is meant to be called in eval mode.")

        if normalize and self.text_normalizer_call is not None:
            str_input = self.text_normalizer_call(str_input, **self.text_normalizer_call_kwargs)

        if self.learn_alignment:
            eval_phon_mode = contextlib.nullcontext()
            if hasattr(self.vocab, "set_phone_prob"):
                eval_phon_mode = self.vocab.set_phone_prob(prob=1.0)

            # Disable mixed g2p representation if necessary
            with eval_phon_mode:
                tokens = self.parser(str_input)
        else:
            tokens = self.parser(str_input)

        x = torch.tensor(tokens).unsqueeze_(0).long().to(self.device)
        return x

    def configure_optimizers(self):
        gen_params = chain(
            self.fastpitch.parameters()
        )
        disc_params = chain(self.discriminator.parameters())
        opt1 = torch.optim.AdamW(disc_params, lr=self._cfg.optim.lr)
        opt2 = torch.optim.AdamW(gen_params, lr=self._cfg.optim.lr)
        num_procs = self._trainer.num_devices * self._trainer.num_nodes
        num_samples = len(self._train_dl.dataset)
        batch_size = self._train_dl.batch_size
        iter_per_epoch = np.ceil(num_samples / (num_procs * batch_size))
        max_steps = iter_per_epoch * self._trainer.max_epochs
        logging.info(f"MAX STEPS: {max_steps}")
        sch1 = NoamAnnealing(opt1, d_model=1, warmup_steps=1000, max_steps=max_steps, last_epoch=-1)
        sch1_dict = {
            'scheduler': sch1,
            'interval': 'step',
        }
        sch2 = NoamAnnealing(opt2, d_model=1, warmup_steps=1000, max_steps=max_steps, last_epoch=-1)
        sch2_dict = {
            'scheduler': sch2,
            'interval': 'step',
        }
        return [opt1, opt2], [sch1_dict, sch2_dict]

    @typecheck(
        input_types={
            "text": NeuralType(('B', 'T_text'), TokenIndex()),
            "durs": NeuralType(('B', 'T_text'), TokenDurationType()),
            "pitch": NeuralType(('B', 'T_audio'), RegressionValuesType()),
            "speaker": NeuralType(('B'), Index(), optional=True),
            "pace": NeuralType(optional=True),
            "spec": NeuralType(('B', 'D', 'T_spec'), MelSpectrogramType(), optional=True),
            "attn_prior": NeuralType(('B', 'T_spec', 'T_text'), ProbsType(), optional=True),
            "mel_lens": NeuralType(('B'), LengthsType(), optional=True),
            "input_lens": NeuralType(('B'), LengthsType(), optional=True),
        }
    )
    def forward(
        self,
        *,
        text,
        durs=None,
        pitch=None,
        speaker=None,
        pace=1.0,
        spec=None,
        attn_prior=None,
        mel_lens=None,
        input_lens=None,
    ):
        return self.fastpitch(
            text=text,
            durs=durs,
            pitch=pitch,
            speaker=speaker,
            pace=pace,
            spec=spec,
            attn_prior=attn_prior,
            mel_lens=mel_lens,
            input_lens=input_lens,
        )

    @typecheck(output_types={"spect": NeuralType(('B', 'D', 'T_spec'), MelSpectrogramType())})
    def generate_spectrogram(
        self, tokens: 'torch.tensor', speaker: Optional[int] = None, pace: float = 1.0
    ) -> torch.tensor:
        if self.training:
            logging.warning("generate_spectrogram() is meant to be called in eval mode.")
        if isinstance(speaker, int):
            speaker = torch.tensor([speaker]).to(self.device)
        spect, *_ = self(text=tokens, durs=None, pitch=None, speaker=speaker, pace=pace)
        return spect

    def adversarial_loss(self, pred, labels: int):
        if isinstance(pred, list):
            pred = torch.vstack(pred)
        if labels == 1:
            label_tensor = torch.ones(pred.size(0), 1)
        elif labels == 0:
            label_tensor = torch.zeros(pred.size(0), 1)
        else:
            raise ValueError("labels can take value either 0 or 1 but got {}".format(labels))
        label_tensor = label_tensor.type_as(pred)
        return torch.nn.functional.binary_cross_entropy(pred, label_tensor)


    def training_step(self, batch, batch_idx, optimizer_idx):
        attn_prior, durs, speaker = None, None, None
        if self.learn_alignment:
            if self.ds_class_name == "TTSDataset":
                if SpeakerID in self._train_dl.dataset.sup_data_types_set:
                    audio, audio_lens, text, text_lens, attn_prior, pitch, _, speaker = batch
                else:
                    audio, audio_lens, text, text_lens, attn_prior, pitch, _ = batch
            else:
                raise ValueError(f"Unknown vocab class: {self.vocab.__class__.__name__}")
        else:
            audio, audio_lens, text, text_lens, durs, pitch, speaker = batch

        mels, spec_len = self.preprocessor(input_signal=audio, length=audio_lens)

        # train discriminator
        if optimizer_idx == 0:
            with torch.no_grad():
                mels_pred, _, _, log_durs_pred, pitch_pred, attn_soft, attn_logprob, attn_hard, attn_hard_dur, pitch = self(
                    text=text,
                    durs=durs,
                    pitch=pitch,
                    speaker=speaker,
                    pace=1.0,
                    spec=mels if self.learn_alignment else None,
                    attn_prior=attn_prior,
                    mel_lens=spec_len,
                    input_lens=text_lens,
                )

            preds = []
            for m in mels_pred:
                start = np.random.randint(low=0, high=int(m.size(1) - self.splice_length))
                preds.append(m[:, start : start + self.splice_length])
            preds = torch.stack(preds)
            gt_mels = []
            for m in mels:
                start = np.random.randint(low=0, high=int(m.size(1) - self.splice_length))
                gt_mels.append(m[:, start : start + self.splice_length])
            gt_mels = torch.stack(gt_mels)

            real_score = self.discriminator(spect=gt_mels)
            gen_score = self.discriminator(spect=preds.detach())

            loss_real = self.adversarial_loss(
                pred=real_score, labels=1
            )
            loss_gen = self.adversarial_loss(
                pred=gen_score, labels=0
            )
            loss_disc = loss_gen + loss_real

            self.log("loss_discriminator", loss_disc, prog_bar=True)
            self.log("loss_discriminator_real", loss_real)
            self.log("loss_discriminator_generated", loss_gen)
            return loss_disc

        # train generator
        elif optimizer_idx == 1:
            mels_pred, _, _, log_durs_pred, pitch_pred, attn_soft, attn_logprob, attn_hard, attn_hard_dur, pitch = self(
                text=text,
                durs=durs,
                pitch=pitch,
                speaker=speaker,
                pace=1.0,
                spec=mels if self.learn_alignment else None,
                attn_prior=attn_prior,
                mel_lens=spec_len,
                input_lens=text_lens,
            )

            preds = []
            for m in mels_pred:
                start = np.random.randint(low=0, high=int(m.size(1) - self.splice_length))
                preds.append(m[:, start : start + self.splice_length])
            preds = torch.stack(preds)

            gen_score = self.discriminator(spect=preds)
            if durs is None:
                durs = attn_hard_dur

            mel_loss = self.mel_loss(spect_predicted=mels_pred, spect_tgt=mels)
            dur_loss = self.duration_loss(log_durs_predicted=log_durs_pred, durs_tgt=durs, len=text_lens)
            gen_loss = self.adversarial_loss(
                pred=gen_score, labels=1
            )
            loss = mel_loss + dur_loss + gen_loss
            if self.learn_alignment:
                ctc_loss = self.forward_sum_loss(attn_logprob=attn_logprob, in_lens=text_lens, out_lens=spec_len)
                bin_loss_weight = min(self.current_epoch / self.bin_loss_warmup_epochs, 1.0) * 1.0
                bin_loss = self.bin_loss(hard_attention=attn_hard, soft_attention=attn_soft) * bin_loss_weight
                loss += ctc_loss + bin_loss

            pitch_loss = self.pitch_loss(pitch_predicted=pitch_pred, pitch_tgt=pitch, len=text_lens)
            loss += pitch_loss

            self.log("t_loss", loss)
            self.log("t_mel_loss", mel_loss)
            self.log("t_dur_loss", dur_loss)
            self.log("t_pitch_loss", pitch_loss)
            self.log("t_loss_generator", gen_loss)
            if self.learn_alignment:
                self.log("t_ctc_loss", ctc_loss)
                self.log("t_bin_loss", bin_loss)

            # Log images to tensorboard
            if self.log_train_images and isinstance(self.logger, TensorBoardLogger):
                self.log_train_images = False

                self.tb_logger.add_image(
                    "train_mel_target",
                    plot_spectrogram_to_numpy(mels[0].data.cpu().float().numpy()),
                    self.global_step,
                    dataformats="HWC",
                )
                spec_predict = mels_pred[0].data.cpu().float().numpy()
                self.tb_logger.add_image(
                    "train_mel_predicted", plot_spectrogram_to_numpy(spec_predict), self.global_step, dataformats="HWC",
                )
                if self.learn_alignment:
                    attn = attn_hard[0].data.cpu().float().numpy().squeeze()
                    self.tb_logger.add_image(
                        "train_attn", plot_alignment_to_numpy(attn.T), self.global_step, dataformats="HWC",
                    )
                    soft_attn = attn_soft[0].data.cpu().float().numpy().squeeze()
                    self.tb_logger.add_image(
                        "train_soft_attn", plot_alignment_to_numpy(soft_attn.T), self.global_step, dataformats="HWC",
                    )
            elif isinstance(self.logger, WandbLogger):
                self.log_train_images = False
                specs = []
                alignments = []

                specs += [
                    wandb.Image(
                        plot_spectrogram_to_numpy(mels_pred[0].data.cpu().float().numpy()), 
                        caption=f"train_mel_predicted",
                    ),
                    wandb.Image(
                        plot_spectrogram_to_numpy(mels[0].data.cpu().float().numpy()),
                        caption=f"train_mel_target",
                    ),
                ]

                if self.learn_alignment:
                    attn = attn_hard[0].data.cpu().float().numpy().squeeze()
                    soft_attn = attn_soft[0].data.cpu().float().numpy().squeeze()
                    alignments += [
                        wandb.Image(
                            plot_alignment_to_numpy(attn.T), caption=f"train_attn",
                        ),
                        wandb.Image(
                            plot_alignment_to_numpy(soft_attn.T),
                            caption=f"train_soft_attn",
                        ),
                    ]
                self.logger.experiment.log({"specs": specs, "alignments": alignments})
                
            return loss

    def validation_step(self, batch, batch_idx):
        attn_prior, durs, speaker = None, None, None
        if self.learn_alignment:
            if self.ds_class_name == "TTSDataset":
                if SpeakerID in self._train_dl.dataset.sup_data_types_set:
                    audio, audio_lens, text, text_lens, attn_prior, pitch, _, speaker = batch
                else:
                    audio, audio_lens, text, text_lens, attn_prior, pitch, _ = batch
            else:
                raise ValueError(f"Unknown vocab class: {self.vocab.__class__.__name__}")
        else:
            audio, audio_lens, text, text_lens, durs, pitch, speaker = batch

        mels, mel_lens = self.preprocessor(input_signal=audio, length=audio_lens)

        # Calculate val loss on ground truth durations to better align L2 loss in time
        mels_pred, _, _, log_durs_pred, pitch_pred, _, _, _, attn_hard_dur, pitch = self(
            text=text,
            durs=durs,
            pitch=pitch,
            speaker=speaker,
            pace=1.0,
            spec=mels if self.learn_alignment else None,
            attn_prior=attn_prior,
            mel_lens=mel_lens,
            input_lens=text_lens,
        )

        preds = []
        for m in mels_pred:
            start = np.random.randint(low=0, high=int(m.size(1) - self.splice_length))
            preds.append(m[:, start : start + self.splice_length])
        preds = torch.stack(preds)

        gen_score = self.discriminator(spect=preds)
        if durs is None:
            durs = attn_hard_dur

        mel_loss = self.mel_loss(spect_predicted=mels_pred, spect_tgt=mels)
        dur_loss = self.duration_loss(log_durs_predicted=log_durs_pred, durs_tgt=durs, len=text_lens)
        pitch_loss = self.pitch_loss(pitch_predicted=pitch_pred, pitch_tgt=pitch, len=text_lens)
        gen_loss = self.adversarial_loss(
                pred=gen_score, labels=1
            )
        loss = mel_loss + dur_loss + pitch_loss + gen_loss

        return {
            "val_loss": loss,
            "mel_loss": mel_loss,
            "dur_loss": dur_loss,
            "pitch_loss": pitch_loss,
            "loss_generator": gen_loss,
            "mel_target": mels if batch_idx == 0 else None,
            "mel_pred": mels_pred if batch_idx == 0 else None,
        }

    def validation_epoch_end(self, outputs):
        collect = lambda key: torch.stack([x[key] for x in outputs]).mean()
        val_loss = collect("val_loss")
        mel_loss = collect("mel_loss")
        dur_loss = collect("dur_loss")
        gen_loss = collect("loss_generator")
        pitch_loss = collect("pitch_loss")
        self.log("v_loss", val_loss)
        self.log("v_mel_loss", mel_loss)
        self.log("v_dur_loss", dur_loss)
        self.log("v_loss_generator", gen_loss)
        self.log("v_pitch_loss", pitch_loss)

        _, _, _, _, _, spec_target, spec_predict = outputs[0].values()

        if isinstance(self.logger, TensorBoardLogger):
            self.tb_logger.add_image(
                "val_mel_target",
                plot_spectrogram_to_numpy(spec_target[0].data.cpu().float().numpy()),
                self.global_step,
                dataformats="HWC",
            )
            spec_predict = spec_predict[0].data.cpu().float().numpy()
            self.tb_logger.add_image(
                "val_mel_predicted", plot_spectrogram_to_numpy(spec_predict), self.global_step, dataformats="HWC",
            )
            self.log_train_images = True
        elif isinstance(self.logger, WandbLogger):
            fastpitch_log_to_wandb_func(self.logger.experiment, outputs[0].values(), self.global_step, tag="val", griffin_lim_power=2)
            self.log_train_images = True

    def __setup_dataloader_from_config(self, cfg, shuffle_should_be: bool = True, name: str = "train"):
        if "dataset" not in cfg or not isinstance(cfg.dataset, DictConfig):
            raise ValueError(f"No dataset for {name}")
        if "dataloader_params" not in cfg or not isinstance(cfg.dataloader_params, DictConfig):
            raise ValueError(f"No dataloder_params for {name}")
        if shuffle_should_be:
            if 'shuffle' not in cfg.dataloader_params:
                logging.warning(
                    f"Shuffle should be set to True for {self}'s {name} dataloader but was not found in its "
                    "config. Manually setting to True"
                )
                with open_dict(cfg.dataloader_params):
                    cfg.dataloader_params.shuffle = True
            elif not cfg.dataloader_params.shuffle:
                logging.error(f"The {name} dataloader for {self} has shuffle set to False!!!")
        elif not shuffle_should_be and cfg.dataloader_params.shuffle:
            logging.error(f"The {name} dataloader for {self} has shuffle set to True!!!")

        if cfg.dataset._target_ == "nemo.collections.tts.torch.data.TTSDataset":
            phon_mode = contextlib.nullcontext()
            if hasattr(self.vocab, "set_phone_prob"):
                phon_mode = self.vocab.set_phone_prob(prob=None if name == "val" else self.vocab.phoneme_probability)

            with phon_mode:
                dataset = instantiate(
                    cfg.dataset,
                    text_normalizer=self.normalizer,
                    text_normalizer_call_kwargs=self.text_normalizer_call_kwargs,
                    text_tokenizer=self.vocab,
                )
        else:
            dataset = instantiate(cfg.dataset)

        return torch.utils.data.DataLoader(dataset, collate_fn=dataset.collate_fn, **cfg.dataloader_params)

    def setup_training_data(self, cfg):
        self._train_dl = self.__setup_dataloader_from_config(cfg)

    def setup_validation_data(self, cfg):
        self._validation_dl = self.__setup_dataloader_from_config(cfg, shuffle_should_be=False, name="val")

    def setup_test_data(self, cfg):
        """Omitted."""
        pass

    @classmethod
    def list_available_models(cls) -> 'List[PretrainedModelInfo]':
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.
        Returns:
            List of available pre-trained models.
        """
        list_of_models = []
        model = PretrainedModelInfo(
            pretrained_model_name="tts_en_fastpitch",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/tts_en_fastpitch/versions/1.8.1/files/tts_en_fastpitch_align.nemo",
            description="This model is trained on LJSpeech sampled at 22050Hz with and can be used to generate female English voices with an American accent.",
            class_=cls,
        )
        list_of_models.append(model)

        return list_of_models

    def convert_text_to_waveform(self, *, tokens):
        """
        Accepts tokens returned from self.parse() and returns a list of tensors. Note: The tensors in the list can have
        different lengths.
        """
        self.eval()
        audio, _, log_dur_pred, _ = self(text=tokens, splice=False)
        audio = audio.squeeze(1)
        durations = torch.sum(torch.clamp(torch.exp(log_dur_pred) - 1, 0, self.max_token_duration), 1).to(torch.int)
        audio_list = []
        for i, sample in enumerate(audio):
            audio_list.append(sample[: durations[i] * self.hop_size])

        return audio_list

    @property
    def input_types(self):
        return self._input_types

    @property
    def output_types(self):
        return self._output_types