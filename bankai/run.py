"""Dev/prod entrypoint: python run.py"""
import uvicorn

from bankai import config

if __name__ == "__main__":
    uvicorn.run("bankai.app:app", host="0.0.0.0", port=config.PORT)
