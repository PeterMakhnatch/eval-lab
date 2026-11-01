# serving

`serving/harness.py::serve_turn` is the production model -> tool -> model path (non-streaming, monitored).
`reference_v2/loop.py` is the streaming loop the new serving stack uses, written against a plain `DefaultContext`.
Tools live in `serving/tools.py`; `serving/model.py` is a scripted model for tests.
