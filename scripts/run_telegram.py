# scripts/run_telegram.py
import runpy

if __name__ == "__main__":
    runpy.run_module("src.telegram_bot", run_name="__main__")
