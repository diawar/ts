FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 TENDER_BASE_DIR=/app
WORKDIR /app
COPY pyproject.toml ./
RUN python -m pip install --upgrade "pip>=26.3,<27"
RUN python -c "import tomllib,subprocess,sys; subprocess.check_call([sys.executable,'-m','pip','install',*tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']])"
COPY src ./src
COPY extensions ./extensions
COPY manage.py ./
RUN pip install --no-deps . && DJANGO_SECRET_KEY=build-time-static-only TENDER_SQLITE=1 python manage.py collectstatic --noinput
RUN useradd --create-home --uid 10001 app && mkdir -p /app/data && chown app:app /app/data
USER app
EXPOSE 8000
CMD ["gunicorn", "tender_search.web.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "2", "--timeout", "60", "--access-logfile", "-", "--access-logformat", "%(t)s %(m)s %(s)s %(L)s"]
