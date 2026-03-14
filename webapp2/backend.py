# main.py
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import FileResponse,HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from fastapi.templating import Jinja2Templates
from sqlmodel import SQLModel, Field, create_engine, Session, select
import secrets, time, os
import uvicorn

DB_FILE = "kaikari.db"
templates = Jinja2Templates(directory="templates")
engine = create_engine(f"sqlite:///{DB_FILE}", echo=False, connect_args={"check_same_thread": False})

app = FastAPI(title="KaikariXpress API")

# allow local dev from any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# serve static files from ./static
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")


# ---- DB Models ----
class Product(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    price: int
    category: Optional[str] = None
    img: Optional[str] = None
    inventory: int = 100

class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    phone: str
    token: Optional[str] = None

class OTP(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    phone: str
    code: str
    expires_at: int

class Address(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int
    line: str
    pincode: str

class OrderItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    order_id: int
    product_id: int
    qty: int
    price_each: int

class Order(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int
    address_id: int
    status: str = "placed"
    total: int = 0
    created_at: int = Field(default_factory=lambda: int(time.time()))

SQLModel.metadata.create_all(engine)


# ---- Utility helpers ----
def seed_products():
    with Session(engine) as session:
        existing = session.exec(select(Product)).first()
        if existing:
            return
        items = [
            {"name":"Tomato","price":30,"category":"Vegetable","img":"https://i.imgur.com/8Km9tLL.jpg"},
            {"name":"Potato","price":20,"category":"Vegetable","img":"https://i.imgur.com/6L89ZbT.jpg"},
            {"name":"Onion","price":40,"category":"Vegetable","img":"https://i.imgur.com/1U5Y2Wk.jpg"},
            {"name":"Carrot","price":50,"category":"Vegetable","img":"https://i.imgur.com/KZsmUi2.jpg"},
            {"name":"Spinach","price":24,"category":"Leafy","img":"https://i.imgur.com/ebFZ0ka.jpg"},
        ]
        for it in items:
            p = Product(**it)
            session.add(p)
        session.commit()

seed_products()


def get_user_from_token(request: Request):
    auth = request.headers.get("authorization")
    if not auth or not auth.startswith("Bearer "):
        return None
    token = auth.split(" ",1)[1]
    with Session(engine) as session:
        user = session.exec(select(User).where(User.token == token)).first()
        return user

# ---- Schemas ----
class OTPRequest(BaseModel):
    phone: str

class OTPVerify(BaseModel):
    phone: str
    otp: str

class AddressIn(BaseModel):
    line: str
    pincode: str

class OrderItemIn(BaseModel):
    product_id: int
    qty: int

class OrderIn(BaseModel):
    address_id: int
    items: List[OrderItemIn]
    location: Optional[dict] = None

# ---- API Endpoints ----
@app.get("/api/products")
def list_products():
    with Session(engine) as session:
        prods = session.exec(select(Product)).all()
        return [p.dict() for p in prods]

@app.post("/api/send_otp")
def send_otp(req: OTPRequest):
    phone = req.phone.strip()
    if len(phone) != 10:
        raise HTTPException(status_code=400, detail="invalid phone")
    code = f"{secrets.randbelow(900000)+100000}"  # 6-digit
    expires = int(time.time()) + 300
    with Session(engine) as session:
        otp = OTP(phone=phone, code=code, expires_at=expires)
        session.add(otp)
        session.commit()
    # In production send via SMS provider. For dev we log to console.
    print(f"[DEV OTP] phone={phone} otp={code}")
    return {"message":"otp_sent"}

@app.post("/api/verify_otp")
def verify_otp(req: OTPVerify):
    with Session(engine) as session:
        row = session.exec(select(OTP).where(OTP.phone==req.phone).order_by(OTP.id.desc())).first()
        if not row or row.code != req.otp or row.expires_at < int(time.time()):
            raise HTTPException(status_code=400, detail="invalid otp")
        # create or find user & assign token
        user = session.exec(select(User).where(User.phone==req.phone)).first()
        token = secrets.token_hex(16)
        if not user:
            user = User(phone=req.phone, token=token)
            session.add(user)
        else:
            user.token = token
            session.add(user)
        session.commit()
    return {"token": token}

@app.post("/api/addresses")
def create_address(addr: AddressIn, req: Request):
    user = get_user_from_token(req)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    with Session(engine) as session:
        a = Address(user_id=user.id, line=addr.line, pincode=addr.pincode)
        session.add(a); session.commit()
        addrs = session.exec(select(Address).where(Address.user_id==user.id)).all()
        return [r.dict() for r in addrs]

@app.get("/api/addresses")
def list_addresses(req: Request):
    user = get_user_from_token(req)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    with Session(engine) as session:
        addrs = session.exec(select(Address).where(Address.user_id==user.id)).all()
        return [r.dict() for r in addrs]

@app.post("/api/orders")
def create_order(data: OrderIn, req: Request):
    user = get_user_from_token(req)
    if not user:
        raise HTTPException(status_code=401, detail="unauthorized")
    with Session(engine) as session:
        # compute total & create order
        total = 0
        for it in data.items:
            prod = session.exec(select(Product).where(Product.id==it.product_id)).first()
            if not prod: raise HTTPException(status_code=400, detail=f"product {it.product_id} missing")
            total += prod.price * it.qty
        order = Order(user_id=user.id, address_id=data.address_id, total=total)
        session.add(order); session.commit()
        for it in data.items:
            prod = session.exec(select(Product).where(Product.id==it.product_id)).first()
            oi = OrderItem(order_id=order.id, product_id=it.product_id, qty=it.qty, price_each=prod.price)
            session.add(oi)
        session.commit()
        return {"order_id": order.id, "status":"placed"}

@app.post("/api/location")
def save_location(loc: dict, req: Request):
    # in a real app you'd store user's last known location; here we just accept it if user is logged in
    user = get_user_from_token(req)
    if not user:
        # allow anonymous location in dev (no token required)
        return {"message":"location_received"}
    return {"message":"location_saved"}

# Simple admin endpoint to list orders
@app.get("/api/admin/orders")
def list_orders():
    with Session(engine) as session:
        orders = session.exec(select(Order)).all()
        out = []
        for o in orders:
            items = session.exec(select(OrderItem).where(OrderItem.order_id==o.id)).all()
            out.append({"order": o.dict(), "items":[it.dict() for it in items]})
        return out

# serve index.html at root if exists in static or project root
@app.get("/",response_class=HTMLResponse)
def root():
    
    with open("index2.html", "r", encoding="utf-8") as f:
        return f.read()
if __name__ == "__main__":
    uvicorn.run(app, host="16.171.235.104", port=8000)