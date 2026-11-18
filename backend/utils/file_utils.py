import uuid

def save_file(content: str):
    filename = f"{uuid.uuid4()}.v"
    with open(filename, "w") as f:
        f.write(content)
    return filename