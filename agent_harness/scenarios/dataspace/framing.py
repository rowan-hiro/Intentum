"""What the model is told about a DataSpace task, on top of the backend's own MCP instructions.

The wording is part of the measured conditions (see README.md); change it
only as a recorded experiment.
"""

TASK_FRAMING = """\
You are a data agent solving one analytics task. Work only through the tools.

- Before importing or exploring, read the question once and make declare_output your first tool call.
  Declare exactly the ordered answer columns the question asks for; do not include helper, grouping or
  identifier columns unless the question explicitly asks for them.
- The task workspace directory is: {context_dir}
  It contains csv/json/sqlite files and a knowledge.md that describes them. Import it first.
- Produce the answer as a table with exactly the columns the question asks for, then write it with
  export_result to this path (overwrite allowed): {prediction_path}
- When the file has been written, reply with the single word DONE.
"""

DELIVERED_PROMPT = "The prediction file has been written. Reply DONE if you are finished, or continue if you still want to change it."

NUDGE_PROMPT = "Continue with the tools. Reply DONE only after export_result has written the prediction file."
