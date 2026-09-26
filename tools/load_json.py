import json 
from collections import OrderedDict


def dir_load_json(path):
    
    with open(path,'r') as f:
        data = json.load(f)
        
    return data 

def str_load_json(str):
    data = json.loads(str)
    return data  

