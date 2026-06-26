from fastapi import FastAPI
from fastapi.routing import APIRoute


def list_routes(app: FastAPI) -> str:
    lines = [f"This is the {app.title}", "", "Routes:"]
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        doc_lines = (route.endpoint.__doc__ or "").strip().splitlines()
        doc = doc_lines[0] if doc_lines else ""
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"}):
            lines.append(f"{method:<7}{route.path:<50}{doc}")
    return "\n".join(lines)
