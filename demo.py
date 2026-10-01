from app import VersionedVault
v=VersionedVault('demo-vault'); v.put('mode','safe'); v.put('region','east'); print(v.versions())
