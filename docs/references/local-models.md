# Local models

Syll talks to LLMs through [LiteLLM](https://docs.litellm.ai/), so anything LiteLLM can reach — hosted APIs or local OpenAI-compatible servers — works with the same config shape.

## Configuration

All model endpoints live under `models` in `~/.syll/config.json`. The two required slots are `chat` (the agent's main brain) and optionally `vision` (for screenshots and GUI tasks). Example:

```json
{
  "models": {
    "chat": {
      "model": "openai/gpt-4o",
      "api_base": "http://localhost:8000/v1",
      "api_key": "not-needed"
    }
  }
}
```

The `model` string follows LiteLLM's naming: `<provider>/<model-name>`. For any OpenAI-compatible server (vLLM, Ollama, LM Studio, llama.cpp server, Text Generation WebUI), use the `openai/` prefix and set `api_base` to your server URL.

## vLLM

```bash
pip install vllm
python -m vllm.entrypoints.openai.api_server \
  --model meta-llama/Llama-3.1-8B-Instruct \
  --port 8000
```

Config:
```json
{
  "models": {
    "chat": {
      "model": "openai/meta-llama/Llama-3.1-8B-Instruct",
      "api_base": "http://localhost:8000/v1",
      "api_key": "EMPTY"
    }
  }
}
```

## Ollama

```bash
ollama pull llama3.1:8b
ollama serve
```

Config (LiteLLM has a native Ollama provider):
```json
{
  "models": {
    "chat": {
      "model": "ollama/llama3.1:8b",
      "api_base": "http://localhost:11434"
    }
  }
}
```

## LM Studio

Enable the local server in LM Studio (default port 1234).

```json
{
  "models": {
    "chat": {
      "model": "openai/local-model",
      "api_base": "http://localhost:1234/v1",
      "api_key": "lm-studio"
    }
  }
}
```

## llama.cpp server

```bash
./llama-server -m model.gguf --port 8080
```

```json
{
  "models": {
    "chat": {
      "model": "openai/local",
      "api_base": "http://localhost:8080/v1",
      "api_key": "not-needed"
    }
  }
}
```

## Notes on tool use

Syll's agent loop depends on function-calling. Small local models often struggle with structured tool-call emission — expect reliability to drop as the model shrinks. If the agent stops calling tools, check the model's tool-calling support before blaming Syll.

Models that work well in testing:
- Any Claude / GPT-4-class hosted model (recommended default)
- Llama 3.1 70B and above via vLLM
- Qwen 2.5 32B and above

Smaller models are fine for chat-only use without tools — disable skills that require tool calls and set a simpler system prompt.
