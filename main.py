from flask import Flask, render_template_string, jsonify

app = Flask(__name__)

# Simple clean page
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MrBlack Bot - Live</title>
    <style>
        body { margin:0; font-family:Arial; background:#f0f2f5; height:100vh; display:flex; flex-direction:column; align-items:center; justify-content:center; }
        h1 { color:#007bff; }
        p { font-size:1.2rem; }
    </style>
</head>
<body>
    <h1>MrBlack Bot</h1>
    <p>Site is now LIVE ✅</p>
    <p>Baad mein chat aur trading interface add karenge</p>
</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/health")
def health():
    return jsonify({"health": "good", "status": "live"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
