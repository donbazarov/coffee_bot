import os

# Имя итогового файла, который вы загрузите в Gemini
OUTPUT_FILE = "project_context.txt"

# Что мы строго игнорируем, чтобы не раздувать контекст мусором
IGNORE_DIRS = {
    '.git', '.venv', 'venv', 'env', '__pycache__', 
    '.vscode', '.idea', 'node_modules', 'static/uploads'
}
IGNORE_FILES = {
    OUTPUT_FILE, 'bundle_code.py', '.DS_Store', 
    'db.sqlite3', 'project.db', 'local.db'
}
# Разрешенные расширения файлов для анализа архитектуры
VALID_EXTENSIONS = {
    '.py', '.html', '.css', '.js', '.json', 
    '.md', '.env', '.clinerules', 'Dockerfile'
}

def build_context():
    count = 0
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as out:
        out.write("=== АРХИТЕКТУРА И КОД ПРОЕКТА ШИФТ-МЕНЕДЖЕРА ===\n\n")
        
        for root, dirs, files in os.walk('.'):
            # Фильтруем папки-мусор
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            
            for file in files:
                if file in IGNORE_FILES:
                    continue
                
                # Проверяем расширение или точное имя (например, Dockerfile)
                _, ext = os.path.splitext(file)
                if ext in VALID_EXTENSIONS or file in VALID_EXTENSIONS:
                    file_path = os.path.join(root, file)
                    relative_path = os.path.relpath(file_path, '.')
                    
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        # Делаем красивую разметку для Gemini
                        out.write(f"// ==========================================\n")
                        out.write(f"// ФАЙЛ: {relative_path}\n")
                        out.write(f"// ==========================================\n\n")
                        out.write(content)
                        out.write("\n\n")
                        count += 1
                    except Exception as e:
                        print(f"Пропущен файл {relative_path} из-за ошибки чтения: {e}")
                        
    print(f"🎉 Успешно! Склеено файлов: {count}. Результат сохранен в '{OUTPUT_FILE}'.")

if __name__ == "__main__":
    build_context()