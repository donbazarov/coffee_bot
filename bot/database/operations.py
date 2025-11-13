from sqlalchemy.orm import Session
from .models import SessionLocal, DrinkReview

def save_review(review_data: dict):
    """Сохранение оценки в базу данных"""
    db = SessionLocal()
    try:
        review = DrinkReview(**review_data)
        db.add(review)
        db.commit()
        db.refresh(review)
        return review
    finally:
        db.close()

def get_reviews_by_barista(barista_name: str):
    """Получение всех оценок для бариста"""
    db = SessionLocal()
    try:
        return db.query(DrinkReview).filter(DrinkReview.barista_name == barista_name).all()
    finally:
        db.close()

# Другие функции для работы с БД...