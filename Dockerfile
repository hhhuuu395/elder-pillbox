# ModelScope Docker 创空间用。平台规定对外端口固定 7860，且必须监听 0.0.0.0。
#
# 这里直接用 uvicorn 起 app:app，app.py 末尾那段 __main__ 块不会执行，
# 所以云端这套启动方式和本地 `python app.py` 互不干扰 —— app.py 一行都不用改。
FROM python:3.11-slim

WORKDIR /home/user/app

# 依赖单独一层：以后只改 app.py 时这层能命中缓存，不必重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
