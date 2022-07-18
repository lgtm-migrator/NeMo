import json
import os
import re
from glob import glob

from data_preparation_utils import check_data, get_wordid_to_nemo_cmu, is_valid, post_process, setup_tokenizer
from nemo_text_processing.text_normalization.normalize import Normalizer
from tqdm import tqdm

from nemo.collections.tts.torch.g2p_utils.data_utils import correct_wikihomograph_data, read_wikihomograph_file

def pre_process(text):
    text = text.replace("é", "e")
    return text

def post_process_normalization(text):
    text = (
        text.replace("slash ", "/ ")
        .replace("–", "-")
        .replace(" slash[", " slash [")
        .replace("—", "-")
        .replace("C₃", "C three")
        .replace("I²C", "I two C")
        .replace("α-methyl", "alpha methyl")
        .replace(" /ʔ/", "")
        .replace("B₁₀₅", "B one hundred and five")
        .replace("B₄₈", "B forty eight")
        .replace("B₂₈-B-B₂₈ (B₅₇)", "B twenty eight B B twenty eight (B fifty seven)")
        .replace("NF-κB", "Nuclear factor kappa B")
        .replace("PLCγ", "Phosphoinositide phospholipase gamma")
        .replace("#½", "")
        .replace("pᵢ", "p i")
        .replace("RSO₂OH", "R S O two O H")
        .replace(" 2ᵃˣⁱˢ", "")
        .replace(": Гугутка", "")
        .replace(" ܕ, d-, da-.", "")
        .replace(" A⁰", "")
        .replace("17β", "seventeen beta")
        .replace(" (英米本位の平和主義を排す)", "")
        .replace(" (Russian: рекрутская повинность)", "")
        .replace("πλατυς ", "")
        .replace("(じいちゃん Jiichan)", "")
        .replace("WCl₆", "W C L six")
        .replace(" Ca(C₁₀H₁₂O₄N₅PO₄)", " shown in the book")
        .replace(" (bda)Fe(CO)₃", "")
        .replace("Tris(pentafluorophenyl)borane", "Tris (pentafluorophenyl) borane")
        .replace(" (C₆F₅)₃B", "")
        .replace(" with the formula Na₂HPO₄", "")
        .replace("ǀXam, ǂUngkue", "Xam, Ungkue")
        .replace("Ytterbium(III) chloride (YbCl₃)", "Ytterbium chloride")
        .replace(" with the formula Cl₂O₇", "")
        .replace(" with formula (CH₃)₂C₆H₃NH₂", "")
        .replace("C₆F₅XeF", "this one")
        .replace("C₄H₄S", "this one")
        .replace("PbCrO₄", "this one")
        .replace("PbS₂", "this one")
        .replace("Cd(CH₃)₂", "shown in the book")
        .replace("β", "beta")
        .replace("MΩ ", "")
        .replace("K₂(C₂H₄O(COO)₂)", "mentioned above")
        .replace(" (NI₃)", "")
        .replace(" with formula KC₅H₈NO₄", "")
        .replace(" Gin'iro Doresu (銀色ドレス Silver Dress)", " Silver Dress")
        .replace("ρ-", "p ")
        .replace(
            'σ2B (the "Breeding expectations" variance) and σ2δ (the "Breeding deviations" variance)',
            'the "Breeding expectations" variance and the "Breeding deviations" variance',
        )
        .replace(" CaCl₂", "")
        .replace("copper(II) acetate to copper(I) oxide (Cu₂O)", "copper acetate to copper oxide")
        .replace("CuSO₄", "C u S O four")
        .replace("The function eᵃˣ", "This function")
        .replace("R⁴", "R to the forth power")
        .replace("₀", " zero ")
        .replace("₁", " one ")
        .replace("₂", " two ")
        .replace("₃", " three ")
        .replace("₄", " four")
        .replace("₅", " five ")
        .replace("₆", " six ")
        .replace("₇", " seven ")
        .replace("δ-", "delta")
        .replace("πr²", "pi r squared")
        .replace("km³", "cubic kilometers")
        .replace("CU mi", "cubic miles")
        .replace("³", " cubed")
        .replace("ʻo", "")
        .replace(" (Hangul: 화경숙빈최씨; Hanja: 和瓊淑嬪崔氏)", "")
        .replace("(Bulgarian: Смилцена) ", "")
        .replace("(Мисс CCCP) ", "")
        .replace("σ", " sigma ")
        .replace("α", " alpha ")
        .replace("γ", " gamma ")
        .replace("κ", " kappa ")
        .replace("μeff", "effective magnetic moment")
        .replace("Београдски Синдикат", "")
        .replace("(モンキーマジック) ", "")
        .replace("P⁵", "P to the fifth power")
        .replace("ΛᵏV", "lambda V")
        .replace("I-695", "I-six hundred ninety five")
        .replace("501", "five hundred and one")  # TN bug
        .replace("2009", "twenty oh nine")  # TN bug
        .replace("ᵢ", " i ")
        .replace("µg/mL", "microgram per milliliter")
        .replace("ρε", "re")
        .replace("ς", "s")
        .replace("for g¹(q;τ)", "")
        .replace("G(Γ)", "G")
        .replace("π", "pi")
        .replace(":جامعة المغتربين", "")
        .replace("TᵏM", "T M")
    )

    return text.replace("  ", " ")


def remove_cjk(text):
    """
    Remove CJK symbols, that are OOV for English G2P
    The 4E00—9FFF range covers CJK Unified Ideographs (CJK=Chinese, Japanese and Korean).
    """
    cjk = [n for n in re.findall(r'[\u4e00-\u9fff]+', text)]
    for cjk_ in cjk:
        text = text.replace(cjk_, "")
    return text


def normalize_wikihomograph_data(subset, post_fix, data_folder="data"):
    BASE_DIR = "/home/ebakhturina/g2p_scripts/"
    output_dir = f"{BASE_DIR}/WikipediaHomographData-master/{data_folder}/{subset}_{post_fix}"
    os.makedirs(output_dir, exist_ok=True)

    normalizer = Normalizer(lang="en", input_case="cased")
    num_removed = 0

    for file in tqdm(glob(f"{BASE_DIR}/WikipediaHomographData-master/{data_folder}/{subset}/*.tsv")):
        file_name = os.path.basename(file)
        output_f = f"{output_dir}/{file_name.replace('.tsv', '.json')}"
        if os.path.exists(output_f):
            continue

        sentences, start_end_indices, homographs, word_ids = read_wikihomograph_file(file)
        with open(output_f, "w") as f_out:
            for i, sent in enumerate(sentences):
                start, end = start_end_indices[i]
                sent, start, end = correct_wikihomograph_data(sent, start, end)
                sent = pre_process(sent)
                homograph = file_name.replace(".tsv", "")

                replace_token = "[]"
                homograph_span = sent[start:end]
                if homograph_span.lower() != homograph and sent.lower().count(homograph) == 1:
                    start = sent.lower().index(homograph)
                    end = start + len(homograph)
                    homograph_span = sent[start:end].lower()
                    assert homograph == homograph_span.lower()

                # we'll skip examples where start/end indices are incorrect and
                # the target homorgaph is also present in the context (ambiguous)
                if homograph_span.lower() != homograph:
                    import pdb

                    pdb.set_trace()
                    num_removed += 1
                else:
                    sentence_to_normalize = sent[: int(start)] + replace_token + sent[int(end) :]
                    try:
                        norm_text = normalizer.normalize(
                            text=sentence_to_normalize, verbose=False, punct_post_process=True, punct_pre_process=True,
                        )
                    except:
                        print("TN ERROR: ", sentence_to_normalize)
                        num_removed += 1

                    norm_text = post_process_normalization(norm_text)
                    entry = {
                        "text_graphemes": norm_text,
                        "norm_text_graphemes": norm_text.replace(replace_token, homograph_span),
                        "start_end": [start, end],
                        "homograph_span": homograph_span,
                        "word_id": word_ids[i],
                    }
                    f_out.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"Normalized data is saved at {output_dir}, number of removed lines: {num_removed}")


def _prepare_wikihomograph_data(post_fix, output_dir, phoneme_dict, split, data_folder="data"):
    drop = []
    replace_token = "[]"
    # to replace heteronyms with correct IPA form
    wordid_to_nemo_cmu = get_wordid_to_nemo_cmu("/home/ebakhturina/NeMo/examples/tts/G2P/data/wordid_to_nemo_cmu.tsv")
    ipa_tok = setup_tokenizer(phoneme_dict=phoneme_dict)
    normalized_data = f"/home/ebakhturina/g2p_scripts/WikipediaHomographData-master/{data_folder}/{split}_{post_fix}/"
    files = glob(f"{normalized_data}/*.json")

    os.makedirs(output_dir, exist_ok=True)
    manifest = f"{output_dir}/{split}_wikihomograph.json"
    with open(manifest, "w", encoding="utf-8") as f_out:
        for file in tqdm(files):
            with open(file, "r") as f_in:
                for line in f_in:
                    line = json.loads(line)
                    graphemes = line["text_graphemes"]
                    # TODO remove this: duplicate of normalization, here for debugging with pickled normalization
                    # graphemes = post_process_normalization(graphemes)
                    graphemes = remove_cjk(graphemes)
                    if not is_valid(graphemes):
                        drop.append(graphemes_)

                    else:
                        ipa_, graphemes_ = ipa_tok(graphemes, wordid_to_nemo_cmu)
                        graphemes_ = graphemes_.replace(replace_token, line["homograph_span"])
                        heteronym_ipa = wordid_to_nemo_cmu[line["word_id"]]
                        ipa_ = ipa_.replace(replace_token, heteronym_ipa)
                        graphemes_ = post_process(graphemes_)

                        line["text_graphemes"] = graphemes_
                        line["text"] = post_process(ipa_)
                        line["duration"] = 0.001
                        line["audio_filepath"] = "n/a"
                        f_out.write(json.dumps(line, ensure_ascii=False) + "\n")
        print(
            f"During validation check in dataset preparation dropped: {len(drop)}, Data for {split.upper()} saved at {manifest}"
        )
        return manifest


def prepare_wikihomograph_data(post_fix, output_dir, split, phoneme_dict=None, data_folder="data"):
    if phoneme_dict is None:
        phoneme_dict = "/home/ebakhturina/NeMo/scripts/tts_dataset_files/ipa_cmudict-0.7b_nv22.06.txt"
    normalize_wikihomograph_data(split, post_fix, data_folder=data_folder)
    manifest = _prepare_wikihomograph_data(post_fix, split=split, output_dir=output_dir, phoneme_dict=phoneme_dict, data_folder=data_folder)
    print('checking..')
    check_data(manifest)


if __name__ == "__main__":
    split = "eval"
    post_fix = "normalized_3"
    output_dir = "TMP"
    prepare_wikihomograph_data(post_fix, output_dir=output_dir, split=split)