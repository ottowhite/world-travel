PORT ?= 8765

.PHONY: serve download

# Serve the interactive viewer (reads crops from data/ on demand).
serve:
	uv run scripts/serve.py --port $(PORT)

# Download the CHELSA pr + tas GeoTIFFs into data/ (~6 GB).
download:
	uv run scripts/download_data.py
