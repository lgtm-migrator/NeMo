python prepare_big_data_for_punctuation_capitalization_task_simple.py \
  --output_dir /media/apeganov/DATA/europarl_92_128_29.11.2021 \
  --corpus_types europarl \
  --create_model_input \
  --bert_labels \
  --autoregressive_labels \
  --sequence_length_range 92 128 \
  --allowed_punctuation '.,?' \
  --only_first_punctuation_character_after_word_in_autoregressive \
  --no_label_if_all_characters_are_upper_case \
  --input_files ~/data/europarl/v10/training-monolingual/europarl-v10.en.tsv \
  --num_jobs 24 \
  --num_passes_through_dataset 3 \
  --dev_size 10000