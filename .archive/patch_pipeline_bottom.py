import sys

with open("Project_Meridian/scripts/daily_pipeline.py", "r") as f:
    content = f.read()

# The file has:
# if __name__ == '__main__':
#     ...
# def _phase_overnight():
#     ...

parts = content.split("if __name__ == '__main__':")
if len(parts) == 2:
    top = parts[0]
    bottom = "if __name__ == '__main__':" + parts[1]
    
    # We appended the functions at the end of the file.
    # Let's find "def _phase_overnight():" in bottom.
    bottom_parts = bottom.split("def _phase_overnight():")
    if len(bottom_parts) == 2:
        real_bottom = bottom_parts[0]
        funcs = "def _phase_overnight():" + bottom_parts[1]
        
        # Put funcs before if __name__
        new_content = top + funcs + "\n" + real_bottom
        with open("Project_Meridian/scripts/daily_pipeline.py", "w") as f:
            f.write(new_content)
        print("Patched successfully!")
    else:
        print("Could not find _phase_overnight in bottom")
else:
    print("Could not split by if __name__")

