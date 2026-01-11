#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Скрипт обрабатывает файл, в каждой строке которого записано одно
пятибуквенное русское слово.
Что делает скрипт:
    1. Читает исходный файл (имя запрашивается у пользователя
       или передаётся в командной строке).
    2. Переводит все буквы в верхний регистр.
    3. Заменяет букву "Ё" на "Е".
    4. Убирает дубликаты, оставляя только первое вхождение
       (т. е. сохраняет порядок появления слов).
    5. Сохраняет результат в файл **New_rus.txt** в той же директории.

Требования:
    * Python 3.6+ (используется only built‑in modules)
    * Файл должен быть в кодировке UTF‑8 (стандарт для современных
      русскоязычных проектов). При необходимости поменяйте параметр
      `encoding`.
"""

import sys
from pathlib import Path


def read_input_path() -> Path:
    """
    Возвращает объект Path с именем исходного файла.
    При запуске из командной строки имя передаётся как первый аргумент.
    Если аргумент не указан – запрашиваем у пользователя через input().
    """
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    else:
        inp = input("Введите имя (или путь) исходного файла: ").strip()
        return Path(inp)


def process_file(src_path: Path, dst_path: Path) -> None:
    """
    Читает src_path → список слов → преобразует их → пишет в dst_path.
    """
    # При чтении/записи явно указываем UTF‑8, чтобы правильно работать с ё/Ё.
    with src_path.open("r", encoding="utf-8") as src:
        # Сохраняем порядок появления слов, убирая дубликаты.
        seen = set()
        result = []                       # список уникальных слов в нужном порядке
        for line in src:
            word = line.strip()          # убираем перевод строки и пробелы по краям
            if not word:                  # пустая строка – игнорируем
                continue
            # 1) переводим в верхний регистр
            word = word.upper()
            # 2) заменяем Ё на Е
            word = word.replace("Ё", "Е")
            # 3) удаляем дубликаты (первое вхождение оставляем)
            if word not in seen:
                seen.add(word)
                result.append(word)

    # Записываем результат
    with dst_path.open("w", encoding="utf-8", newline="\n") as dst:
        for w in result:
            dst.write(w + "\n")


def main() -> None:
    src = read_input_path()
    if not src.is_file():
        sys.exit(f"❌ Ошибка: файл «{src}» не найден или недоступен.")
    dst = src.parent / "New_rus.txt"
    process_file(src, dst)
    print(f"✅ Готово! Результат записан в «{dst}»")


if __name__ == "__main__":
    main()
