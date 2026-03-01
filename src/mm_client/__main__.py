"""Entry point: uv run python -m src.mm_client"""
import asyncio

from src.mm_client.run import main

asyncio.run(main())
