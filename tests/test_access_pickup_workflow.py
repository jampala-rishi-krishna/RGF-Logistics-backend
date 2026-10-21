"""Role, privacy, request/alert, exact PO cutoff and pickup email regressions. No external IO."""
import os
from datetime import date, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import BigInteger, create_engine, Index, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault('JWT_SECRET', 'test-only-secret')
from auth import dependencies, security
from database import get_db
from models.user import User, UserInvite
from models.access_workflow import UnassignRequest, PickupDecision, PickupEmailLog
from routers import access_workflow, admin, assignment, auth, load_planning, pickup
from services import app_settings, assignment_privacy, live_sales_order_cache, pickup_email, pickup_rules, purchase_orders, user_cache


@compiles(JSONB, 'sqlite')
def json_sqlite(t, c, **kw): return 'JSON'

@compiles(BigInteger, 'sqlite')
def bigint_sqlite(t, c, **kw): return 'INTEGER'


@pytest.fixture
def env(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread':False})
    for model in [User, UserInvite, UnassignRequest, PickupDecision, PickupEmailLog]:
        model.__table__.create(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(dependencies, 'SessionLocal', Session)
    user_cache._cache.clear()
    app_settings.reset()
    app = FastAPI(dependencies=[Depends(dependencies.workspace_access)])
    for r in [access_workflow.router, pickup.router, assignment.router, admin.router, admin.usage_router, auth.router, load_planning.router]: app.include_router(r)
    def db():
        with Session() as session: yield session
    app.dependency_overrides[get_db] = db
    ids={}
    with Session() as db:
        for i,role in enumerate(['admin','planner','sales','wet_market','driver','helper','logistics_officer']):
            user=User(full_name=role.title(),email=f'{role}@test.com',role=role,phone=None,status='active',is_manager=role=='admin',password_hash=security.hash_password('A-valid-test-password'))
            db.add(user); db.flush(); ids[role]=user.id
        db.commit()
    def headers(role): return {'Authorization': 'Bearer '+security.create_access_token(user_id=ids[role],role=role,email=f'{role}@test.com')}
    yield SimpleNamespace(client=TestClient(app), Session=Session, ids=ids, headers=headers)
    user_cache._cache.clear(); app_settings.reset(); purchase_orders.reset()


@pytest.mark.parametrize('role', ['sales','wet_market','driver','helper','logistics_officer'])
def test_nonoperations_roles_cannot_unassign_or_bulk(env,role):
    h=env.headers(role)
    assert env.client.post('/api/load-planning/assignments/one/unassign',headers=h,json={}).status_code==403
    assert env.client.post('/api/load-planning/assignments/bulk-unassign',headers=h,json={'order_ids':['one']}).status_code==403


def test_planner_can_unassign_without_manager_permission(env, monkeypatch):
    calls = []
    def single(order_id, user, db, **kwargs):
        calls.append((order_id, user.role, user.is_manager))
        return {'success': True}
    def bulk(order_ids, user, db, **kwargs):
        calls.append((order_ids, user.role, user.is_manager))
        return {'success': True}
    monkeypatch.setattr(assignment, 'unassign_one', single)
    monkeypatch.setattr(assignment, 'bulk_unassign_orders', bulk)
    h = env.headers('planner')
    assert env.client.post('/api/load-planning/assignments/one/unassign', headers=h, json={}).status_code == 200
    assert env.client.post('/api/load-planning/assignments/bulk-unassign', headers=h, json={'order_ids':['one']}).status_code == 200
    preview = env.client.post('/api/load-planning/assignments/unassign-preview', headers=h, json={'order_ids':[]})
    assert preview.status_code == 400 and preview.json()['detail'] == 'Select at least one order.'
    assert calls == [('one', 'planner', False), (['one'], 'planner', False)]


def test_planner_reaches_overview_but_cannot_manage_users_or_settings(env):
    assert env.client.get('/api/admin/settings',headers=env.headers('planner')).status_code==403
    assert env.client.get('/users',headers=env.headers('planner')).status_code==403
    assert dependencies.require_role('admin','dispatcher')(dependencies.CurrentUser(1,'p@x','planner','P','active')).role=='planner'
    for path in ['/api/fleet-health/trucks','/api/officer/pending','/warehouse-events']:
        with pytest.raises(HTTPException) as forbidden:
            dependencies.workspace_access(SimpleNamespace(url=SimpleNamespace(path=path)), dependencies.CurrentUser(1,'p@x','planner','P','active'))
        assert forbidden.value.status_code==403


@pytest.mark.parametrize('role',['sales','wet_market'])
def test_sales_readonly_and_manager_cannot_be_self_granted(env,role):
    h=env.headers(role)
    assert env.client.post('/api/load-planning/inventory/sales-orders/1/acknowledge',headers=h).status_code==403
    assert env.client.patch(f'/api/admin/users/{env.ids[role]}/manager',headers=h,json={'is_manager':True}).status_code==403
    user=dependencies.CurrentUser(1,'s@x',role,'S','active')
    for path in ['/api/load-planning/inventory/sales-orders','/api/load-planning/inventory/export/excel']:
        request=SimpleNamespace(method='GET',url=SimpleNamespace(path=path))
        assert dependencies.require_role('admin','dispatcher')(user,request)==user
        dependencies.workspace_access(request,user)


def test_nullable_phone_email_login_and_seed_on_create(env):
    h=env.headers('admin')
    for email in ['jed@rareglobalfood.com','pau@rareglobalfood.com','no-phone@test.com']:
        r=env.client.post('/api/admin/users',headers=h,json={'full_name':'New user','email':email,'role':'planner','password':'A-valid-test-password'})
        assert r.status_code==201,r.text
        assert r.json()['phone'] is None
        assert r.json()['is_manager']==(email!='no-phone@test.com')
        login=env.client.post('/auth/login',json={'identifier':email,'password':'A-valid-test-password'})
        assert login.status_code==200,login.text
    assert env.client.post('/api/admin/users',headers=h,json={'full_name':'Phone','email':'phone@test.com','role':'sales','phone':'09171234567','password':'A-valid-test-password'}).status_code==201
    assert env.client.post('/auth/login',json={'identifier':'+639171234567','password':'A-valid-test-password'}).status_code==200


def test_admin_manager_toggle_invalidates_cached_permission(env):
    uid=env.ids['planner']
    assert not dependencies._load_user(uid).is_manager
    assert env.client.patch(f'/api/admin/users/{uid}/manager',headers=env.headers('admin'),json={'is_manager':True}).status_code==200
    assert dependencies._load_user(uid).is_manager


def test_attribution_redacts_recursively_and_exports_have_no_columns():
    data={'items':[{'assigned_by':1,'assigned_by_name':'Pau','assigned_at':'now','raw_json':{'assignedBy':'Pau','assignedAt':'now'},'id':'1'}]}
    assert assignment_privacy.redact(data)['items']==[{'raw_json':{},'id':'1'}]
    assert assignment_privacy.redact(data,True)==data
    from services.inventory_exports import HEADERS, CONFIRMED_HEADERS
    assert all('assigned' not in h.lower() for h in HEADERS+CONFIRMED_HEADERS)


@pytest.mark.parametrize('manager',[False,True])
def test_real_json_boundary_hides_attribution_from_nonmanagers_and_exports(monkeypatch,manager):
    import main
    app=FastAPI()
    app.middleware('http')(main.protect_assignment_attribution)
    monkeypatch.setattr(dependencies,'_load_user',lambda uid: SimpleNamespace(is_manager=manager))
    @app.get('/orders')
    @app.get('/inventory/export')
    def rows(): return {'items':[{'id':'one','assigned_by':1,'assigned_by_name':'Pau','assigned_at':'today','raw_json':{'assignedBy':'Pau'}}]}
    token=security.create_access_token(user_id=1,role='planner',email='p@test.com')
    client=TestClient(app)
    headers={'Authorization':f'Bearer {token}'}
    item=client.get('/orders',headers=headers).json()['items'][0]
    assert ('assigned_by_name' in item)==manager
    assert client.get('/inventory/export',headers=headers).json()['items']==[{'id':'one','raw_json':{}}]


@pytest.mark.parametrize('action',['approve','reject'])
def test_request_duplicate_alerts_and_resolution(env,monkeypatch,action):
    order=SimpleNamespace(id='one',salesorder_number='SO26-18449',assignment_status='assigned')
    monkeypatch.setattr(live_sales_order_cache,'find_cached',lambda oid:order)
    monkeypatch.setattr(assignment,'_candidate',lambda *a,**kw:{})
    monkeypatch.setattr(assignment.dispatch_changes,'describe',lambda *a:[{'blocked':False,'needs_confirm':False}])
    monkeypatch.setattr(assignment,'_apply_unassign',lambda o,db:setattr(o,'assignment_status','unassigned'))
    monkeypatch.setattr(assignment,'_record_unassign',lambda *a:None)
    monkeypatch.setattr(assignment,'_after_unassign',lambda:None)
    payload={'order_ids':['one'],'reason':'Wrong truck'}
    assert env.client.post('/api/access/unassign-requests',headers=env.headers('planner'),json=payload).status_code==201
    assert env.client.post('/api/access/unassign-requests',headers=env.headers('planner'),json=payload).status_code==409
    alerts=env.client.get('/api/access/alerts',headers=env.headers('admin')).json()
    assert alerts['unread_count']==1 and 'SO26-18449 (Wrong truck)' in alerts['items'][0]['text']
    rid=alerts['items'][0]['id']
    assert env.client.post(f'/api/access/alerts/{rid}/read',headers=env.headers('sales')).status_code==403
    env.client.post(f'/api/access/alerts/{rid}/read',headers=env.headers('admin'))
    assert env.client.get('/api/access/alerts',headers=env.headers('admin')).json()['unread_count']==0
    r=env.client.post(f'/api/access/unassign-requests/{rid}/resolve',headers=env.headers('admin'),json={'action':action,'note':'Reviewed'})
    assert r.status_code==200,r.text
    outcome=env.client.get('/api/access/alerts',headers=env.headers('planner')).json()
    assert outcome['unread_count']==1 and ('Approved' if action=='approve' else 'Rejected: Reviewed') in outcome['items'][0]['text']


def record(**kw):
    return {'purchaseorder_id':'1','purchaseorder_number':'PO-001','cf_shipment_method':'For Pickup','status':'issued','delivery_date':'2026-10-17','created_time':'2026-10-16T14:00:59+0800',**kw}


@pytest.mark.parametrize('changes,expected',[
    ({},'scheduled'),({'created_time':'2026-10-16T14:01:00+0800'},'pahabol'),
    ({'created_time':'2026-10-16T06:00:59Z'},'scheduled'),({'created_time':'2026-10-16T06:01:00Z'},'pahabol'),
    ({'cf_shipment_method':'Pickup'},'excluded'),({'cf_shipment_method':'for pickup'},'excluded'),
    ({'delivery_date':'2026-10-18'},'excluded'),({'delivery_date':'2026-10-16'},'excluded'),
    ({'delivery_date':None},'excluded'),({'status':'open'},'excluded'),({'created_time':None},'excluded')])
def test_exact_pickup_rules(changes,expected):
    assert pickup_rules.classify(record(**changes),today=date(2026,10,16))==expected


def test_shipping_address_never_billing_and_vendor_lookup_cached(monkeypatch):
    purchase_orders.reset(); calls=[]
    monkeypatch.setattr(purchase_orders,'fetch_vendor_contact',lambda vid:(calls.append(vid),{'contact':{'shipping_address':{'address':'Shipping road'},'billing_address':{'address':'Wrong billing'},'contact_persons':[{'first_name':'Ana','mobile':'09171234567'}]}})[1])
    vendor=purchase_orders.vendor_contact({'vendor_id':'v'})
    assert purchase_orders.vendor_contact({'vendor_id':'v'})==vendor and calls==['v']
    row=purchase_orders.build_row(record(vendor_name='Vendor',vendor_contact=vendor),{'line_items':[]})
    assert row.shipping_address['address']=='Shipping road'
    assert not row.raw_json['vendor_address_missing']
    assert purchase_orders.vendor_address({'billing_address':{'address':'Wrong'}})=={}
    assert purchase_orders.delivery_day({'expected_delivery_date':'2026-10-17'})==(None,False)
    assert pickup_rules.changed_after_cutoff(record(last_modified_time='2026-10-16T14:01:00+0800'))
    assert not pickup_rules.changed_after_cutoff(record(last_modified_time='2026-10-16T14:00:59+0800'))


def test_pahabol_approve_joins_regular_list(env,monkeypatch):
    now=datetime.now(pickup_rules.PHT)
    r=record(delivery_date=(now.date()+timedelta(days=1)).isoformat(),created_time=now.replace(hour=14,minute=1,second=0).isoformat())
    monkeypatch.setattr(purchase_orders,'get_records',lambda **kw:({'1':r},now))
    monkeypatch.setattr(purchase_orders,'cached_detail',lambda *a:{'line_items':[]})
    assert purchase_orders.pickup_state(r)=='pahabol'
    out=env.client.post('/api/pickup/PO:1/decision',headers=env.headers('admin'),json={'action':'approve'})
    assert out.status_code==200,out.text
    assert purchase_orders.pickup_state(r)=='scheduled'
    assert env.client.post('/api/pickup/PO:1/decision',headers=env.headers('admin'),json={'action':'reject'}).status_code==409


def test_email_skip_empty_and_manual_preview_guard(env,monkeypatch):
    data={'delivery_date':'2026-10-17','recipients':{'to':['p@test.com'],'cc':[]},'scheduled':[],'pahabol':[],'preview_token':'same'}
    monkeypatch.setattr(pickup_email,'preview',lambda:data)
    monkeypatch.setattr(pickup_email.gmail_sender,'send_email',lambda **kw:pytest.fail('Empty list must not send'))
    with env.Session() as db:
        assert pickup_email.send(db,preview_token='same')['status']=='skipped_empty'
        with pytest.raises(ValueError): pickup_email.send(db,preview_token='stale')
        assert len(db.execute(select(PickupEmailLog)).scalars().all())==1


def test_email_documents_and_schedule_claim(env,monkeypatch):
    from openpyxl import load_workbook
    data={'delivery_date':'2026-10-17','recipients':{'to':['p@test.com'],'cc':['l@test.com']},'scheduled':[{'po':'PO-1','vendor':'=malicious','address':'Shipping road','contact':'Ana 0917','delivery_date':'2026-10-17','destination':'Mets','items':[{'name':'Pork','qty':1,'unit':'kg','kg':1}]}],'pahabol':[],'preview_token':'same'}
    sent=[]
    monkeypatch.setattr(pickup_email,'preview',lambda:data)
    monkeypatch.setattr(pickup_email.gmail_sender,'send_email',lambda **kw:sent.append(kw))
    with env.Session() as db:
        assert pickup_email.send(db,schedule_key='2026-10-16')['status']=='sent'
        assert pickup_email.send(db,schedule_key='2026-10-16')['status']=='already_attempted'
    assert len(sent)==1 and len(sent[0]['attachments'])==2
    pdf,xlsx=sent[0]['attachments']
    assert pdf[2].startswith(b'%PDF')
    sheet=load_workbook(BytesIO(xlsx[2])).worksheets[0]
    assert sheet['B2'].data_type=='s' and sheet['C2'].value=='Shipping road'
    assert 'assigned' not in str(list(sheet.values)).lower()


def test_scheduler_off_and_exact_manila_minute(env,monkeypatch):
    monkeypatch.setattr(pickup_email,'recipients',lambda:{'to':['p@test.com'],'cc':[]})
    calls=[]
    monkeypatch.setattr(pickup_email,'send',lambda db,**kw:calls.append(kw))
    import database
    monkeypatch.setattr(database,'SessionLocal',env.Session)
    monkeypatch.setattr(pickup_email,'_last_tick',None)
    pickup_email.scheduled_tick(datetime(2026,10,16,14,5,tzinfo=pickup_rules.PHT)); assert calls==[]
    monkeypatch.setitem(app_settings._overrides,'PICKUP_EMAIL_ENABLED','1')
    pickup_email.scheduled_tick(datetime(2026,10,16,14,4,tzinfo=pickup_rules.PHT)); assert calls==[]
    pickup_email.scheduled_tick(datetime(2026,10,16,14,5,tzinfo=pickup_rules.PHT))
    pickup_email.scheduled_tick(datetime(2026,10,16,14,5,30,tzinfo=pickup_rules.PHT))
    assert calls==[{'schedule_key':'2026-10-16'}]
