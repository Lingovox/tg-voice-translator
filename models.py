from datetime import datetime
from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, index=True)
    target_lang: Mapped[str] = mapped_column(String(16), default="en")

    # Маркетинг: 5 бесплатных сообщений (каждое <= 60 сек)
    trial_messages: Mapped[int] = mapped_column(Integer, default=5)

    # Купленные секунды
    balance_seconds: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    invoice_id: Mapped[str] = mapped_column(String(64), default="")
    package_code: Mapped[str] = mapped_column(String(16))
    amount_usd: Mapped[int] = mapped_column(Integer)  # целое число долларов
    status: Mapped[str] = mapped_column(String(32), default="created")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
