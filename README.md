# Opencode Agent Vibe

Autonomous environment for creating interactive web applications through simple instructions.

## Prerequisites

- **Chrome Devtools MCP** installed and registered
- **`OPENROUTER_API_KEY`** in `.env`

## Usage

Open this project in OpenCode and give a simple instruction:

```
Create a mandelbrot explorer.
```

**Quick Start**: Copy and paste any prompt from the `prompt-examples/` directory directly into the OpenCode chat as your first message after opening the project.

The `vibe` agent (defined in `AGENTS.md`) will autonomously create a workspace, build the app, and iterate until visually perfect using browser tools and AI vision.

See `AGENTS.md` for the full agent protocol.
