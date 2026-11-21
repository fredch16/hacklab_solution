# Offline keyword assets

The ONNX weights, `en.phone` lexicon and tokens are from the sherpa-onnx
`sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` release archive. The app uses the
chunk-8, left-64 encoder and joiner (int8) and decoder (float32) under shorter
filenames. Archive SHA-256 is
`68447f4fbc67e70eee3a93961f36e81e98f47aef73ce7e7ca00885c6cd3616a6`.
The model author confirms Apache 2.0. The accompanying CMU pronunciation
dictionary notice is in `CMUDICT-LICENSE`.
[Model archive](https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20.tar.bz2)
[Author and maintainer license statements](https://github.com/k2-fsa/sherpa-onnx/issues/3802)
[CMUdict](https://github.com/cmusphinx/cmudict)

`cmu-lts.bin` is a modified packaging of the Flite 2.2 CMU letter-to-sound
tables by the Language Technologies Institute at Carnegie Mellon University.
It contains the 26 letter roots as little-endian 16-bit offsets, a
NUL-terminated phone list, and the original six-byte decision nodes.
The packer translates AX0 to AH0 for the sherpa token alphabet.
SHA-256 is `e18e63ef1a5d7817bad62ee47d160597405725b0b0231f0c6f4afe03a27c26fc`.
Regenerate it with `python3 apps/mobile/android/tools/pack_flite_lts.py
<flite-v2.2>/lang/cmulex <output>`. The license is in `FLITE-LICENSE`.
[Flite v2.2 source](https://github.com/festvox/flite/tree/v2.2/lang/cmulex)
