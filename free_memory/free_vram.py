import gc
import torch
from typing import Optional

def free_vram(model=None,tokenizer=None):
    # 1. Delete the model and tokenizer variables
    if model:
        del model
    if tokenizer:
        del tokenizer  # If you are using one

    # 2. Run the Python garbage collector
    gc.collect()

    # 3. Clear the PyTorch VRAM cache
    torch.cuda.empty_cache()
