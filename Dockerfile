FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY personal_os_agent/ personal_os_agent/
RUN pip install --no-cache-dir .

EXPOSE 8080

CMD ["python", "-m", "personal_os_agent"]
