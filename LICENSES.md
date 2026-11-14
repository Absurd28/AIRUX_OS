# Licences of shipped third-party components

| Component | Use | Licence | Source |
| --- | --- | --- | --- |
| Qwen3.5 2B / 4B / 9B (Q4_K_M GGUF, Unsloth builds) | Language model | Apache-2.0 | https://huggingface.co/unsloth |
| llama.cpp v1.0.0 | Model server | MIT | https://github.com/ggml-org/llama.cpp |
| Kokoro-82M v1.0 | Text to speech (af_heart default) | Apache-2.0 | https://huggingface.co/hexgrad/Kokoro-82M |
| kokoro-onnx model export | ONNX build of Kokoro | MIT | https://github.com/thewh1teagle/kokoro-onnx |
| whisper.cpp models (base.en, small.en) | Speech to text | MIT | https://huggingface.co/ggerganov/whisper.cpp |

Each shipped voice gets its own row once phase 5 picks them (Kokoro presets, and the Piper fallback voice, whose licence is set per voice).
