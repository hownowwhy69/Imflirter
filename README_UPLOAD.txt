UPLOAD THESE FILES TO GITHUB REPO ROOT (not inside a folder)

1. bot.py
2. requirements.txt
3. Procfile                  <-- HAAN, yeh bhi upload karo
4. render.yaml
5. runtime.txt
6. python-version.txt        <-- iska NAAM badal ke .python-version karo

IMPORTANT:
python-version.txt ko rename karke EXACT yeh naam do:

    .python-version

Pehle ek DOT (.) hai. Windows/phone pe hidden file ho jati hai, isliye zip me
dikh nahi rahi thi. GitHub pe "Create new file" se bhi bana sakte ho:

    File name:  .python-version
    Content:    3.12

Procfile ka content:
    web: python bot.py

Render Dashboard -> Environment me bhi add karo:
    PYTHON_VERSION = 3.12.10
    QT_QPA_PLATFORM = offscreen

Phir Clear build cache & deploy.
