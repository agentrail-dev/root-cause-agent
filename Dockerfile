FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1 BACKEND_URL=http://localhost:8000
EXPOSE 8501
# Backend (interne :8000) + Streamlit (exposé :8501)
CMD ["sh", "-c", "uvicorn backend.main:app --host 127.0.0.1 --port 8000 & exec streamlit run frontend/app.py --server.port=${PORT:-8501} --server.address=0.0.0.0"]
