# wsgi.py
from app.main import create_app

# Crée une instance de ton application Flask
app = create_app()

# Optionnel : permet de lancer le serveur manuellement en local
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)