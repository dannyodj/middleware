#!/usr/bin/env python3

# License: BSD

import pytest
import sys
import os
from time import sleep
from pytest_dependency import depends
apifolder = os.getcwd()
sys.path.append(apifolder)
from auto_config import ip, user, password, pool_name, hostname, scale
from functions import PUT, POST, GET, SSH_TEST, DELETE

try:
    Reason = 'BSD host configuration is missing in ixautomation.conf'
    from config import BSD_HOST, BSD_USERNAME, BSD_PASSWORD
    bsd_host_cfg = pytest.mark.skipif(False, reason=Reason)
except ImportError:
    bsd_host_cfg = pytest.mark.skipif(True, reason=Reason)

MOUNTPOINT = f'/tmp/iscsi-{hostname}'
global DEVICE_NAME
DEVICE_NAME = ""
target_name = "target0"
basename = "iqn.2005-10.org.freenas.ctl"


def test_01_Add_iSCSI_initiator():
    payload = {
        'comment': 'Default initiator',
    }
    results = POST("/iscsi/initiator/", payload)
    assert results.status_code == 200, results.text
    assert isinstance(results.json(), dict), results.text


def test_02_Add_ISCSI_portal():
    global portal_id
    payload = {
        'listen': [
            {
                'ip': '0.0.0.0',
                'port': 3260
            }
        ]
    }
    results = POST("/iscsi/portal/", payload)
    assert results.status_code == 200, results.text
    assert isinstance(results.json(), dict), results.text
    portal_id = results.json()['id']


# Add iSCSI target and group
def test_03_Add_ISCSI_target():
    global target_id
    payload = {
        'name': target_name,
        'groups': [
            {'portal': portal_id}
        ]
    }
    results = POST("/iscsi/target/", payload)
    assert results.status_code == 200, results.text
    assert isinstance(results.json(), dict), results.text
    target_id = results.json()['id']


# Add iSCSI extent
def test_04_Add_ISCSI_extent(request):
    depends(request, ["pool_04"], scope="session")
    global extent_id
    payload = {
        'type': 'FILE',
        'name': 'extent',
        'filesize': 536870912,
        'path': f'/mnt/{pool_name}/dataset03/iscsi'
    }
    results = POST("/iscsi/extent/", payload)
    assert results.status_code == 200, results.text
    assert isinstance(results.json(), dict), results.text
    extent_id = results.json()['id']


# Associate iSCSI target
def test_05_Associate_ISCSI_target(request):
    depends(request, ["pool_04"], scope="session")
    global associate_id
    payload = {
        'target': target_id,
        'lunid': 1,
        'extent': extent_id
    }
    results = POST("/iscsi/targetextent/", payload)
    assert results.status_code == 200, results.text
    assert isinstance(results.json(), dict), results.text
    associate_id = results.json()['id']


# Enable the iSCSI service
def test_06_Enable_iSCSI_service(request):
    depends(request, ["pool_04"], scope="session")
    payload = {"enable": True}
    results = PUT("/service/id/iscsitarget/", payload)
    assert results.status_code == 200, results.text


def test_07_start_iSCSI_service(request):
    depends(request, ["pool_04"], scope="session")
    result = POST(
        '/service/start', {
            'service': 'iscsitarget',
        }
    )
    assert result.status_code == 200, result.text
    sleep(1)


def test_08_Verify_the_iSCSI_service_is_enabled(request):
    depends(request, ["pool_04"], scope="session")
    results = GET("/service/?service=iscsitarget")
    assert results.status_code == 200, results.text
    assert results.json()[0]["state"] == "RUNNING", results.text


# when SSH_TEST is functional test using it will need to be added
# Now connect to iSCSI target
@bsd_host_cfg
def test_09_Connecting_to_iSCSI_target(request):
    depends(request, ["pool_04"], scope="session")
    cmd = f'iscsictl -A -p {ip}:3260 -t {basename}:{target_name}'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.timeout(15)
def test_10_Waiting_for_iscsi_connection_before_grabbing_device_name():
    while True:
        cmd = f'iscsictl -L | grep {ip}:3260'
        results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
        assert results['result'] is True, results['output']
        iscsictl_list = results['output'].strip().split()
        if iscsictl_list[2] == "Connected:":
            global DEVICE_NAME
            DEVICE_NAME = iscsictl_list[3]
            assert True
            break
        sleep(3)


@bsd_host_cfg
def test_11_Format_the_target_volume(request):
    cmd = f'umount "/media/{DEVICE_NAME}"'
    SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    cmd2 = f'newfs "/dev/{DEVICE_NAME}"'
    results = SSH_TEST(cmd2, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_12_Creating_iSCSI_mountpoint(request):
    depends(request, ["pool_04"], scope="session")
    cmd = f'mkdir -p {MOUNTPOINT}'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_13_Mount_the_target_volume(request):
    depends(request, ["pool_04"], scope="session")
    cmd = f'mount "/dev/{DEVICE_NAME}" "{MOUNTPOINT}"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_14_Creating_file(request):
    depends(request, ["pool_04"], scope="session")
    cmd = 'touch "%s/testfile"' % MOUNTPOINT
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_15_Moving_file(request):
    depends(request, ["pool_04"], scope="session")
    cmd = 'mv "%s/testfile" "%s/testfile2"' % (MOUNTPOINT, MOUNTPOINT)
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_16_Copying_file(request):
    depends(request, ["pool_04"], scope="session")
    cmd = 'cp "%s/testfile2" "%s/testfile"' % (MOUNTPOINT, MOUNTPOINT)
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_17_Deleting_file(request):
    depends(request, ["pool_04"], scope="session")
    results = SSH_TEST('rm "%s/testfile2"' % MOUNTPOINT,
                       BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
@pytest.mark.skipif(scale, reason='ctladm is not supported on SCALE')
def test_18_verifiying_iscsi_session_on_truenas(request):
    depends(request, ["pool_04", "ssh_password"], scope="session")
    try:
        results = SSH_TEST('ctladm islist', user, password, ip)
        assert results['result'] is True, results['output']
        hostname = SSH_TEST('hostname', BSD_USERNAME, BSD_PASSWORD, BSD_HOST)['output'].strip()
    except AssertionError as e:
        raise AssertionError(f'Could not verify iscsi session on TrueNAS : {e}')
    else:
        assert hostname in results['output'], 'No active session on TrueNAS for iSCSI'


@bsd_host_cfg
def test_19_Unmounting_iSCSI_volume():
    cmd = f'umount "{MOUNTPOINT}"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_20_Removing_iSCSI_volume_mountpoint():
    cmd = 'rm -rf "MOUNTPOINT"'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


@bsd_host_cfg
def test_21_Disconnect_iSCSI_target(request):
    cmd = f'iscsictl -R -t {basename}:{target_name}'
    results = SSH_TEST(cmd, BSD_USERNAME, BSD_PASSWORD, BSD_HOST)
    assert results['result'] is True, results['output']


# Disable the iSCSI service
def test_22_Disable_iSCSI_service(request):
    depends(request, ["pool_04"], scope="session")
    payload = {'enable': False}
    results = PUT("/service/id/iscsitarget/", payload)
    assert results.status_code == 200, results.text


def test_23_stop_iSCSI_service(request):
    depends(request, ["pool_04"], scope="session")
    results = POST(
        '/service/stop/', {
            'service': 'iscsitarget',
        }
    )
    assert results.status_code == 200, results.text
    sleep(1)


def test_24_Verify_the_iSCSI_service_is_disabled(request):
    depends(request, ["pool_04"], scope="session")
    results = GET("/service/?service=iscsitarget")
    assert results.status_code == 200, results.text
    assert results.json()[0]["state"] == "STOPPED", results.text


# Delete iSCSI target and group
def test_25_Delete_associate_ISCSI_target(request):
    depends(request, ["pool_04"], scope="session")
    results = DELETE(f"/iscsi/targetextent/id/{associate_id}/")
    assert results.status_code == 200, results.text
    assert results.json(), results.text


# Delete iSCSI target and group
def test_26_Delete_ISCSI_target(request):
    depends(request, ["pool_04"], scope="session")
    results = DELETE(f"/iscsi/target/id/{target_id}/")
    assert results.status_code == 200, results.text
    assert results.json(), results.text


# Remove iSCSI extent
def test_27_Delete_iSCSI_extent(request):
    depends(request, ["pool_04"], scope="session")
    results = DELETE(f"/iscsi/extent/id/{extent_id}/")
    assert results.status_code == 200, results.text
    assert results.json(), results.text


# Remove iSCSI portal
def test_28_Delete_portal(request):
    depends(request, ["pool_04"], scope="session")
    results = DELETE(f"/iscsi/portal/id/{portal_id}/")
    assert results.status_code == 200, results.text
    assert results.json(), results.text