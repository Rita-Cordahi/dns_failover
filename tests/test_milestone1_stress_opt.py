import os
import shutil
import pytest
import gzip
import brotli
import asyncio
from fastapi import Response
from fastapi.testclient import TestClient
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend
from backend.main import app
from backend import config

# Register temporary test endpoints on the app for compression testing
@app.get("/api/test/empty")
async def get_empty():
    return Response(content=b"", media_type="text/plain")

@app.get("/api/test/small")
async def get_small():
    return Response(content=b"a", media_type="text/plain")

@app.get("/api/test/large")
async def get_large():
    # 2MB response
    return Response(content=b"a" * 2000000, media_type="text/plain")

# Reorder routes so that our test routes are matched before the static files mount at "/"
test_routes = [r for r in app.routes if hasattr(r, "path") and r.path.startswith("/api/test/")]
for r in test_routes:
    app.routes.remove(r)
for r in reversed(test_routes):
    app.routes.insert(0, r)


# Setup fixture to initialize FastAPICache because ASGI direct calls bypass FastAPI's lifespan
@pytest.fixture(autouse=True, scope="module")
def init_cache_for_asgi():
    try:
        FastAPICache.init(InMemoryBackend(), prefix="fastapi-cache")
    except Exception:
        pass


# Helper function to call the app as a raw ASGI application.
# This avoids any automatic client-side decompression, allowing us to inspect the raw bytes.
async def call_asgi(path, accept_encoding=None, auth_token=None):
    headers = []
    if accept_encoding is not None:
        headers.append((b"accept-encoding", accept_encoding.encode("latin1")))
    if auth_token is not None:
        headers.append((b"authorization", f"Bearer {auth_token}".encode("latin1")))
        
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers,
        "query_string": b"",
    }
    
    async def mock_receive():
        return {"type": "http.request", "body": b"", "more_body": False}
        
    response_start = {}
    response_body = []
    
    async def mock_send(message):
        if message["type"] == "http.response.start":
            response_start.update(message)
        elif message["type"] == "http.response.body":
            response_body.append(message.get("body", b""))
            
    await app(scope, mock_receive, mock_send)
    
    full_body = b"".join(response_body)
    headers_dict = {k.lower(): v for k, v in response_start.get("headers", [])}
    
    return response_start.get("status"), headers_dict, full_body


# 1. Compression Middleware Tests

def test_compression_empty_response():
    # Call empty endpoint with gzip and br
    status, headers, body = asyncio.run(call_asgi(
        "/api/test/empty",
        accept_encoding="gzip, br",
        auth_token=config.API_TOKEN
    ))
    assert status == 200
    
    # Check if empty response was compressed
    # BrotliGzipMiddleware has minimum_size = 0.
    # An empty body has length 0. Since body_length (0) < minimum_size (0) is False,
    # it compresses. Let's see what content-encoding is set and if body is valid.
    content_encoding = headers.get(b"content-encoding")
    if content_encoding == b"br":
        decompressed = brotli.decompress(body)
        assert decompressed == b""
    elif content_encoding == b"gzip":
        decompressed = gzip.decompress(body)
        assert decompressed == b""
    else:
        assert body == b""

    # Validate content-length correctness
    if b"content-length" in headers:
        assert int(headers[b"content-length"]) == len(body)


def test_compression_extremely_small_response():
    status, headers, body = asyncio.run(call_asgi(
        "/api/test/small",
        accept_encoding="gzip",
        auth_token=config.API_TOKEN
    ))
    assert status == 200
    assert headers.get(b"content-encoding") == b"gzip"
    
    decompressed = gzip.decompress(body)
    assert decompressed == b"a"
    
    # Validate content-length header matches actual response body size
    assert b"content-length" in headers
    assert int(headers[b"content-length"]) == len(body)


def test_compression_very_large_response():
    status, headers, body = asyncio.run(call_asgi(
        "/api/test/large",
        accept_encoding="br",
        auth_token=config.API_TOKEN
    ))
    assert status == 200
    assert headers.get(b"content-encoding") == b"br"
    
    decompressed = brotli.decompress(body)
    assert decompressed == b"a" * 2000000
    
    # Validate content-length header matches actual response body size
    assert b"content-length" in headers
    assert int(headers[b"content-length"]) == len(body)


def test_compression_invalid_accept_encoding():
    invalid_headers = [
        "invalid-encoding",
        "None",
        "identity",
        "",
        "deflate",
        "deflate, zstd",
        "compress"
    ]
    for encoding in invalid_headers:
        status, headers, body = asyncio.run(call_asgi(
            "/api/test/small",
            accept_encoding=encoding,
            auth_token=config.API_TOKEN
        ))
        assert status == 200
        assert b"content-encoding" not in headers
        assert body == b"a"
        if b"content-length" in headers:
            assert int(headers[b"content-length"]) == len(body)


def test_compression_false_positive_encoding():
    # If the user sends "broccolis", the server should NOT match "br" and should NOT compress it
    status, headers, body = asyncio.run(call_asgi(
        "/api/test/small",
        accept_encoding="broccolis",
        auth_token=config.API_TOKEN
    ))
    assert status == 200
    assert b"content-encoding" not in headers
    assert body == b"a"



def test_compression_multiple_accept_encoding():
    # Test preference order: Brotli ("br") has precedence over gzip in current implementation
    status, headers, body = asyncio.run(call_asgi(
        "/api/test/small",
        accept_encoding="deflate, gzip, br, zstd",
        auth_token=config.API_TOKEN
    ))
    assert status == 200
    assert headers.get(b"content-encoding") == b"br"
    
    # Test when only gzip and other unsupported encodings are present
    status, headers, body = asyncio.run(call_asgi(
        "/api/test/small",
        accept_encoding="deflate, gzip, zstd",
        auth_token=config.API_TOKEN
    ))
    assert status == 200
    assert headers.get(b"content-encoding") == b"gzip"


def test_compression_content_length_correctness():
    # Retrieve health failover endpoint (large JSON response usually) with gzip
    status, headers, body = asyncio.run(call_asgi(
        "/api/v1/health/failover",
        accept_encoding="gzip",
        auth_token=config.API_TOKEN
    ))
    assert status == 200
    assert headers.get(b"content-encoding") == b"gzip"
    
    # Confirm content-length matches the actual raw response length
    cl_header = headers.get(b"content-length")
    assert cl_header is not None
    assert int(cl_header) == len(body)


# 2. Cache-Control Tests

def test_cache_control_case_sensitivity(client):
    # The application mounts "frontend" at "/" using CacheControlledStaticFiles.
    # Let's request the files using different casing for extensions.
    
    # Test HTML extensions (should be no-cache, must-revalidate)
    for html_path in ["/index.html", "/index.HTML", "/index.Html", "/index.hTmL"]:
        response = client.get(html_path)
        assert response.status_code == 200
        assert response.headers.get("Cache-Control") == "no-cache, must-revalidate"
        
    # Test non-HTML extensions (should be public, max-age=31536000, immutable)
    for asset_path in ["/app.js", "/app.JS", "/app.Js", "/index.css", "/index.CSS", "/index.Css"]:
        response = client.get(asset_path)
        assert response.status_code == 200
        assert response.headers.get("Cache-Control") == "public, max-age=31536000, immutable"


def test_cache_control_subdirectories(client):
    # Ensure static files under directories are handled correctly.
    # Let's create a temporary directory and files in the "frontend" folder.
    frontend_dir = "frontend"
    sub_dir = os.path.join(frontend_dir, "test_subdir")
    os.makedirs(sub_dir, exist_ok=True)
    
    temp_html = os.path.join(sub_dir, "nested.HTML")
    temp_js = os.path.join(sub_dir, "nested.JS")
    
    try:
        with open(temp_html, "w") as f:
            f.write("<html><body>Nested</body></html>")
        with open(temp_js, "w") as f:
            f.write("console.log('nested');")
            
        # Request nested HTML
        response_html = client.get("/test_subdir/nested.HTML")
        assert response_html.status_code == 200
        assert response_html.headers.get("Cache-Control") == "no-cache, must-revalidate"
        
        # Request nested JS
        response_js = client.get("/test_subdir/nested.JS")
        assert response_js.status_code == 200
        assert response_js.headers.get("Cache-Control") == "public, max-age=31536000, immutable"
        
    finally:
        # Clean up
        if os.path.exists(sub_dir):
            shutil.rmtree(sub_dir)


def test_cache_control_non_existent_file(client):
    # Verify that a 404 response check for non-existent file doesn't set Cache-Control for successful files.
    response = client.get("/non_existent_file.html")
    assert response.status_code == 404
    
    # Check Cache-Control header. For a 404, we expect Cache-Control is not set to the static file caching headers
    # (or is missing/empty, or does not contain 'public, max-age=31536000, immutable').
    cache_control = response.headers.get("Cache-Control")
    if cache_control:
        assert "public" not in cache_control
        assert "immutable" not in cache_control
