from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()

class User(UserMixin, db.Model):

    __tablename__ = 'users'
    
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='user')  # 'admin' or 'user'
    security_question = db.Column(db.String(200), nullable=True)
    security_answer_hash = db.Column(db.String(256), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    can_view_others = db.Column(db.Boolean, default=False)
    can_edit_others = db.Column(db.Boolean, default=False)
    can_delete_others = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    # Relationship
    # records = db.relationship('GiftRecord', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def set_security_answer(self, answer):
        if answer:
            self.security_answer_hash = generate_password_hash(answer.strip().lower())

    def check_security_answer(self, answer):
        if not self.security_answer_hash or not answer:
            return False
        return check_password_hash(self.security_answer_hash, answer.strip().lower())

    @property
    def is_admin(self):
        return self.role == 'admin'


class GiftRecord(db.Model):
    __tablename__ = 'gift_records'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)           # 姓名 - 必填
    age = db.Column(db.Integer, nullable=True)                 # 年龄 - 可选
    address = db.Column(db.String(256), nullable=True)         # 地址 - 可选
    phone = db.Column(db.String(30), nullable=True)            # 联系电话 - 可选
    amount = db.Column(db.Float, nullable=False)               # 礼金数额 - 必填
    event_reason = db.Column(db.String(128), nullable=False)   # 办席原因 - 必填
    notes = db.Column(db.Text, nullable=True)                  # 备注 - 可选
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    user = db.relationship('User', backref=db.backref('gift_records', lazy=True))
