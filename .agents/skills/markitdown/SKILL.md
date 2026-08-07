---
name: markitdown
description: "Convert a file (PDF, Word, Excel, PowerPoint, Audio, HTML, etc.) to Markdown using Microsoft's MarkItDown."
argument-hint: <file_path>
---

# MarkItDown Skill

You have been activated with the `/markitdown` command. Your task is to convert the user's requested file to Markdown using the `markitdown` tool installed in the local virtual environment `.venv`.

## Instructions
1. Find the input file path from the arguments provided by the user.
2. Verify that the file exists in the workspace.
3. Run the markitdown CLI tool using the python executable in the local virtual environment:
   `./.venv/bin/markitdown <file_path>`
4. Present the converted markdown output to the user.
5. If the user asks to save the output, write it to a file (e.g., `<basename>.md`).
