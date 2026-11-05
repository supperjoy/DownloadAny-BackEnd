def main() -> None:
    import uvicorn

    uvicorn.run("downloadany_backend.app:app", host="127.0.0.1", port=8000, reload=True)
