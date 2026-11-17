FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY agent_browser.py .
COPY dom_adapter.py .
EXPOSE 5010
CMD ["python", "app.py"]
