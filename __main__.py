import asyncio
from app.server import main, DEFAULT_HOST, DEFAULT_PORT

if __name__ == "__main__":
    asyncio.run(main(DEFAULT_HOST, DEFAULT_PORT))
