import os
from pathlib import Path
from dotenv import load_dotenv

# Determine the absolute path to the directory containing this script
# __file__ is the path to app.py; .resolve() makes it absolute; .parent is the directory
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

# Load the .env file from the absolute path calculated above
# This works regardless of which directory you run the script from
load_dotenv(dotenv_path=ENV_PATH, override=True)

print("Please enter your name:")

# Get user input
name = input()

# Retrieve the environment variable (now loaded into os.environ)
test_env = os.environ.get("TEST_ENV", "Not Set")

# Display the output
print(f"Hello, {name}!, the TEST_ENV variable is set to: {test_env}")