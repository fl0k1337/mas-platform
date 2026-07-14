"""Создание пользователя панели (запускать в терминале):
    python create_user.py
Спросит email и пароль (пароль при вводе не отображается — так и задумано)."""

from getpass import getpass

from app import db
from app.auth import hash_password


def main() -> None:
    db.init_db()
    email = input("Email: ").strip()
    if not email or "@" not in email:
        raise SystemExit("Нужен корректный email.")
    if db.get_user_by_email(email):
        raise SystemExit("Такой пользователь уже есть.")
    password = getpass("Пароль (не менее 8 символов, ввод скрыт): ")
    if len(password) < 8:
        raise SystemExit("Слишком короткий пароль.")
    if getpass("Повторите пароль: ") != password:
        raise SystemExit("Пароли не совпадают.")
    db.create_user(email, hash_password(password))
    print(f"Готово: пользователь {email} создан. Заходите в панель.")


if __name__ == "__main__":
    main()
