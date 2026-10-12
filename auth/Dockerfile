FROM python:3.11-slim
RUN pip install fastapi uvicorn
COPY auth.py .
CMD ["uvicorn", "auth:app", "--host", "0.0.0.0", "--port", "9000"]