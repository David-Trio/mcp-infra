import json
from fastapi import FastAPI, Request, Response

app = FastAPI()

def extract_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    # Fallbacks for clients that can't send Authorization
    return (request.headers.get("x-api-key")
            or request.headers.get("x-auth-token")
            or "")

@app.api_route("/verify", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
def verify(request: Request):
    tokens = json.load(open("/tokens/tokens.json"))
    token = extract_token(request)
    if token and token in tokens:
        return Response(status_code=200, headers={"X-Auth-User": tokens[token]})
    return Response(status_code=401)