import sys, os
sys.path.insert(0, os.path.abspath('.'))
from factory import create_app
from extensions import db
import models

app = create_app()

with app.app_context():
    engine = db.engine
    from sqlalchemy import inspect
    inspector = inspect(engine)
    
    db_tables = inspector.get_table_names()
    
    missing_cols = []
    for mapper in db.Model.__subclasses__():
        table_name = mapper.__tablename__
        if table_name not in db_tables:
            missing_cols.append((table_name, "TABLE MISSING IN DB"))
            continue
        
        db_cols = {col['name'] for col in inspector.get_columns(table_name)}
        model_cols = {c.name for c in mapper.__table__.columns}
        
        diff = model_cols - db_cols
        if diff:
            missing_cols.append((table_name, f"Missing columns in DB: {diff}"))
            
    if missing_cols:
        print(f"DISCREPANCIES FOUND ({len(missing_cols)}):")
        for tbl, msg in missing_cols:
            print(f"  Table: {tbl} -> {msg}")
    else:
        print("ALL MODEL COLUMNS ARE PROPERLY SYNCED WITH THE DATABASE!")
