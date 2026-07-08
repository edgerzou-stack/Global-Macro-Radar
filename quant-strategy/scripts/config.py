import os

# Automatically determine the quant-strategy directory as PROJECT_ROOT
# __file__ is quant-strategy/scripts/config.py
# dirname is quant-strategy/scripts
# dirname dirname is quant-strategy
PROJECT_ROOT = os.environ.get("PROJECT_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def get_project_root():
    return PROJECT_ROOT
