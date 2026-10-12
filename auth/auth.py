import json
from fastapi import FastAPI, Request, Response

app = FastAPI()

@app.api_route("/verify", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
def verify(request: Request):
    tokens = json.load(open("/tokens/tokens.json"))  # re-read every request = no restarts
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and header[7:] in tokens:
        return Response(status_code=200, headers={"X-Auth-User": tokens[header[7:]]})
    return Response(status_code=401)