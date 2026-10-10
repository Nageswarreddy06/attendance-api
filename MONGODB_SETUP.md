# MongoDB Atlas setup

1. Open Atlas and select your project and cluster.
2. Choose **Connect → Drivers → Python** and copy the generated connection string. Do not type the host by hand.
3. In the repository root, copy `.env.example` to a file named exactly `.env`.
4. Replace the username, password, and host placeholders in `MONGO_URI` with the values from Atlas. Keep the URI on one line and set `MONGO_DB=attendance_db`.
5. In Atlas **Network Access**, allow your current public IP. Do not expose the cluster to all IP addresses unless you understand the risk.
6. Install packages and start the service:

   ```powershell
   python -m pip install -r requirements.txt
   python -m uvicorn app.main:app --reload --port 8000
   ```

7. Open `http://127.0.0.1:8000/health`. A successful response is `{"status":"ok"}`; `/docs` contains interactive endpoint documentation.

If startup says `MONGO_URI is missing`, confirm the file is named `.env` (not `.env.txt`), is at the repository root, and its first setting starts with `MONGO_URI=`. If the DNS SRV hostname does not exist, copy the Atlas URI again and confirm the cluster is active.

Never commit `.env` or include real credentials in screenshots. Rotate any password that was shared accidentally.
