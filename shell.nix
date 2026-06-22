{ pkgs ? import <nixpkgs> { } }:

# Development shell for the CHELSA climatologies visualisation.
# uv manages the Python toolchain and project dependencies (rasterio, numpy,
# plotly). We only need uv on PATH here; uv downloads a pinned CPython itself.
pkgs.mkShell {
  packages = [
    pkgs.uv
    pkgs.curl   # used by the data download script
    pkgs.cacert # TLS roots for uv/curl HTTPS downloads
  ];

  # Let uv download and manage the interpreter rather than using a nix Python,
  # so the pyproject/uv.lock are the single source of truth.
  env = {
    UV_PYTHON_DOWNLOADS = "automatic";
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
  };
}
