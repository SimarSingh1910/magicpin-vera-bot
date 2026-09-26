FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py conversation_handlers.py ./
COPY vera/ ./vera/

# Hosts inject $PORT; default to 8080 for local runs.
ENV PORT=8080
EXPOSE 8080

# Single worker on purpose: context and conversation state live in process
# memory, so two workers would each hold half the judge's pushes.
CMD ["python", "bot.py"]
