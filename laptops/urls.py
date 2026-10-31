from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", views.sign_in, name="login"),
    path("logout/", views.sign_out, name="logout"),
    path("register/", views.register, name="register"),
    path("overview/", views.overview, name="overview"),
    path("devices/", views.device_inventory, name="devices"),
    path("devices/register/", views.register_device, name="register_device"),
    path("my-devices/", views.my_devices, name="my_devices"),
    path("devices/<int:device_id>/detail/", views.device_detail, name="device_detail"),
    path("assessments/", views.assessments, name="assessments"),
    path("assessments/", views.assessments, name="it_assessments_portal"),
    path("assessments/self/", views.self_assessment, name="self_assessment"),
    path("assessments/my/", views.user_assessments_tab, name="user_assessments_tab"),
    path("assessments/it/", views.it_assessment, name="it_assessment_empty"),
    path("assessments/it/<int:device_id>/", views.it_assessment, name="it_assessment"),
    path("it-admin/api/save-draft/<int:device_id>/", views.save_it_assessment_draft, name="save_it_assessment_draft"),
    path("sanitization/", views.sanitization, name="sanitization"),
    path("certificates/", views.certificates, name="certificates"),
    path("certificates/<int:certificate_id>/qr/", views.certificate_qr, name="certificate_qr"),
    path("repairs/", views.repairs, name="repairs"),
    path("disposition/", views.disposition, name="disposition"),
    path("profile/", views.profile, name="profile"),
    path("profile/admin-access/<int:user_id>/<str:decision>/", views.admin_access_decision, name="admin_access_decision"),
]
