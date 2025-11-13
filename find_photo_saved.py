import os

def search_in_file(filepath, pattern):
    """Ищет паттерн в файле и показывает строку"""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for i, line in enumerate(lines):
                if pattern in line:
                    print(f"❌ Найдено в {filepath}, строка {i+1}: {line.strip()}")
                    return True
    except Exception as e:
        print(f"⚠️ Ошибка чтения {filepath}: {e}")
    return False

print("🔍 Ищем 'photo_saved' в файлах...")

found = False
for root, dirs, files in os.walk('.'):
    for file in files:
        if file.endswith('.py'):
            filepath = os.path.join(root, file)
            if search_in_file(filepath, 'photo_saved'):
                found = True

if not found:
    print("✅ 'photo_saved' нигде не найден")
else:
    print("\n🚨 Нужно исправить файлы, где найден 'photo_saved'")