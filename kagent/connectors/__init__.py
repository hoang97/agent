"""Connector gọi hệ thống thật. Template trong kagent/templates chỉ được trỏ tới module trong package này.

Mỗi handler nhận (params, args): params là tham số của kết nối (đã giải mã secret, có thêm _allow_private),
args là tham số do agent truyền. Lỗi nghiệp vụ báo bằng ConnectorError.
"""
