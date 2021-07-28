cd ~/mwerSegmenter/

workdir=~/data/iwslt/IWSLT-SLT/eval/en-de/IWSLT.tst2019
translations=( translated_transcripts_segmented translated_transcripts_not_segmented )

result_dir="${workdir}/mwerSegmented"
for tr in "${translations[@]}"; do
  for tr_segm in $(ls "${workdir}/${tr}"); do
    for tr_model in $(ls "${workdir}/${tr}/${tr_segm}"); do
      for translated_text in $(ls "${workdir}/${tr}/${tr_segm}/${tr_model}"); do
        ./segmentBasedOnMWER.sh "${workdir}/IWSLT.TED.tst2019.en-de.en.xml" \
          "${workdir}/IWSLT.TED.tst2019.en-de.de.xml" \
          "${workdir}/${tr}/${tr_segm}/${tr_model}/${translated_text}" \
          "${translated_text%.*}" \
          German \
          "${result_dir}/${tr}/${tr_segm}/${tr_model}/${translated_text%.*}.xml" \
          no \
          1
      done
    done
  done
done
