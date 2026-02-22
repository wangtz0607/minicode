# MiniCode

A powerful command-line AI coding agent written in less than 1000 lines of code.

![Demo](assets/demo.gif)

## Installation

```sh
cd /path/to/minicode
uv venv
source .venv/bin/activate
uv pip install -e .
```

## Usage

```sh
export OPENAI_BASE_URL=https://openrouter.ai/api/v1
export OPENAI_API_KEY=sk-123
export MINICODE_MODEL=z-ai/glm-5
export MINICODE_CONTEXT_WINDOW=204800
export TAVILY_BASE_URL=https://api.tavily.com
export TAVILY_API_KEY=tvly-dev-123 # optional, for WebFetch and WebSearch
minicode
```

- An OpenAI-compatible API key is required.
- (Optional) A [Tavily API key](https://www.tavily.com/) is required for WebFetch and WebSearch tools.

## License

MiniCode is licensed under the [MIT license](https://opensource.org/licenses/MIT).
