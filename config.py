"""
Configuration for the Financial Instruments project.
"""
import os

# SEC EDGAR requires a User-Agent with your name and email
SEC_EDGAR_USER_AGENT = "NiklasGawlitza gawlitza.niklas@gmail.com"

# University LLM endpoint (OpenAI-compatible)
LLM_API_BASE = os.getenv("LLM_API_BASE", "https://litellm.s.studiumdigitale.uni-frankfurt.de")
LLM_API_KEY  = os.getenv("LLM_API_KEY",  "sk-CkceqP6ReDDx6cjvM05sjA")
LLM_MODEL    = "mistral-large-3-675b-instruct-2512"

# Paths
DATA_DIR = "data"
CACHE_DIR = "cache"
OUTPUT_DIR = "output"

# Stock list path
STOCK_LIST_PATH = "data/stock_list.xlsx"
