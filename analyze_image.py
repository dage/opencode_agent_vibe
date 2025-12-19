"""Image analysis tool using vision language models via OpenRouter.

This module provides autonomous UI debugging capabilities by analyzing screenshots
and verifying expected UI states, detecting visual bugs, and providing actionable
feedback for iterative development.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from controllers.env_loader import load_environment
from controllers.openrouter_client import OpenRouterClient, OpenRouterError


def analyze_image(
    image_path: str,
    prompt: str,
    model: str = "qwen/qwen3-vl-235b-a22b-instruct",
) -> str:
    """Analyze an image using a vision language model via OpenRouter.

    This function is designed for AI agents to autonomously verify UI state,
    detect visual bugs, and iterate on fixes without human intervention.

    Args:
        image_path: Path to the image file to analyze (local file path)
        prompt: Detailed analysis prompt describing what to look for.
                Should include specific expectations about UI elements,
                their states, and what constitutes success vs. failure.
        model: Vision model ID to use (default: qwen/qwen3-vl-235b-a22b-instruct)

    Returns:
        Analysis text from the vision language model describing what it sees
        and whether the image matches the expected state described in the prompt

    Raises:
        OpenRouterError: If the API request fails after retries
        ValueError: If the image file doesn't exist or is invalid

    Example:
        >>> result = analyze_image(
        ...     image_path="/tmp/screenshot.png",
        ...     prompt="Check if the Symbol dropdown is populated with at least 3 "
        ...            "stock ticker options (e.g., AAPL, MSFT, GOOGL). Report if "
        ...            "empty or shows 'Loading...'."
        ... )
        >>> print(result)
        "The Symbol dropdown is visible and populated with 5 stock tickers: ..."

    Prompt Engineering Tips:
        - Be specific about what elements to check (dropdowns, buttons, tables, etc.)
        - Provide expected states (e.g., "should contain at least N items")
        - Mention what success looks like AND what failure looks like
        - Ask for specific measurements when relevant (size, count, visibility)
        - Request exact error text if errors are expected to be present
    """
    # Load environment variables from .env if not already loaded
    load_environment()

    # Validate image exists
    path = Path(image_path)
    if not path.exists():
        raise ValueError(f"Image file does not exist: {image_path}")

    # Initialize OpenRouter client
    client = OpenRouterClient()

    # Execute vision API call
    response = client.chat_with_vision(
        text=prompt,
        images=[image_path],
        model=model,
    )

    # Extract assistant message content
    try:
        choices = response.get("choices", [])
        if not choices:
            raise OpenRouterError("No response choices returned from vision model")

        message = choices[0].get("message", {})
        content = message.get("content")

        if content is None:
            raise OpenRouterError("Vision model returned empty content")

        return str(content)

    except (KeyError, IndexError, AttributeError) as e:
        raise OpenRouterError(f"Failed to parse vision model response: {e}") from e


def main() -> int:
    """CLI Entry point."""
    parser = argparse.ArgumentParser(description="Analyze an image using AI vision.")
    parser.add_argument("image_path", help="Path to the image file")
    parser.add_argument("prompt", help="Question or prompt about the image")
    parser.add_argument(
        "--model", 
        default="qwen/qwen3-vl-235b-a22b-instruct", 
        help="Vision model to use"
    )

    args = parser.parse_args()

    try:
        result = analyze_image(args.image_path, args.prompt, model=args.model)
        print(result)
        return 0
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
