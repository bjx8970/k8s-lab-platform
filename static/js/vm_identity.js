/* VM identity is (PVE provider, VMID). A node is only a mutable locator. */
var VMIdentity = (function() {
    'use strict';

    function positiveId(value, label) {
        if (typeof value === 'string' && /^[1-9][0-9]*$/.test(value) && String(Number(value)) === value) {
            value = Number(value);
        }
        if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) {
            throw new Error((label || '资源编号') + '必须是有效的正整数');
        }
        return value;
    }

    // Pure helpers also run under Node, without a document, socket or fetch.
    function key(serverId, vmid) {
        return 'pve-' + positiveId(serverId, 'PVE 服务器编号') + '-vm-' + positiveId(vmid, 'VMID');
    }

    function identity(vm) {
        if (!vm || typeof vm !== 'object' || Array.isArray(vm)) {
            throw new Error('虚拟机身份信息无效');
        }
        var serverId = positiveId(vm.pve_server_id, 'PVE 服务器编号');
        var vmid = positiveId(vm.vmid, 'VMID');
        var canonicalKey = key(serverId, vmid);
        ['key', 'vm_key'].forEach(function(field) {
            if (Object.prototype.hasOwnProperty.call(vm, field) && vm[field] !== canonicalKey) {
                throw new Error('虚拟机身份键与 PVE 服务器编号或 VMID 不一致');
            }
        });
        return {pve_server_id: serverId, vmid: vmid, key: canonicalKey};
    }

    function actionUrl(serverId, vmid, action) {
        if (['start', 'stop', 'reboot'].indexOf(action) < 0) {
            throw new Error('无效的虚拟机操作');
        }
        var vm = identity({pve_server_id: serverId, vmid: vmid});
        return '/api/pve/servers/' + vm.pve_server_id + '/vms/' + vm.vmid + '/' + action;
    }

    function clusterProvider(clusters, name, suppliedId) {
        if (!clusters || !Object.prototype.hasOwnProperty.call(clusters, name) || !clusters[name]) {
            throw new Error('集群身份信息不可用，请刷新后重试');
        }
        var serverId = positiveId(clusters[name].pve_server_id, '集群 PVE 服务器编号');
        // Only a cluster-only event may omit its provider; never infer a VM's provider.
        if (suppliedId !== undefined && positiveId(suppliedId, 'PVE 服务器编号') !== serverId) {
            throw new Error('集群 PVE 服务器身份不匹配，请刷新后重试');
        }
        return serverId;
    }

    function clusterPayload(clusters, name) {
        var serverId = clusterProvider(clusters, name);
        Object.values(clusters[name].vms || {}).forEach(function(vm) {
            if (identity(vm).pve_server_id !== serverId) {
                throw new Error('虚拟机与集群的 PVE 服务器身份不一致');
            }
        });
        return {cluster_name: name, pve_server_id: serverId};
    }

    function shutdownIdentity(data, clusters) {
        if (!data || typeof data !== 'object') throw new Error('关机身份信息无效');
        if (data.type === 'cluster') {
            return {pve_server_id: clusterProvider(clusters, data.cluster_name, data.pve_server_id)};
        }
        return identity(data);
    }

    function statusInfo(result) {
        result = result || {};
        if (result.error || result.success === false || result.status === 'error' || result.status === 'failed') {
            return {status: 'error', label: '状态查询失败' + (typeof result.error === 'string' ? '：' + result.error : '')};
        }
        if (result.status === 'running') return {status: 'running', label: '运行中'};
        if (result.status === 'stopped') return {status: 'stopped', label: '已停止'};
        return {status: 'unknown', label: '状态未知'};
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    function renderTag(vm, label, clusterName, cache, canAct, voteOnStop) {
        var ref;
        try {
            ref = identity(vm);
        } catch (error) {
            return '<span class="vm-tag vm-unknown">' + escapeHtml(label) +
                ' <span class="vm-status text-danger">身份无效：' + escapeHtml(error.message) + '</span></span>';
        }
        var cached = cache[ref.key];
        var state = typeof cached === 'string' ? statusInfo({status: cached}) : (cached || statusInfo());
        var html = '<span class="vm-tag vm-' + state.status + '" data-vm-key="' + ref.key +
            '" data-cluster-name="' + escapeHtml(clusterName) + '">' + escapeHtml(label) +
            ' <span class="vm-status">' + escapeHtml(state.label) + '</span>';
        if (canAct) {
            var args = ref.pve_server_id + ',' + ref.vmid;
            html += '<button class="vm-btn" onclick="event.stopPropagation();vmAction(' + args + ',\'start\')">&#9654;</button>' +
                '<button class="vm-btn" onclick="event.stopPropagation();vmAction(' + args + ',\'stop\'' +
                (voteOnStop ? ',this.parentNode.dataset.clusterName' : '') + ')">&#9209;</button>';
        }
        return html + '</span>';
    }

    function updateStatus(data, cache, root) {
        var ref = identity(data); // Validate before using any key in a selector or cache.
        var state = statusInfo(data);
        root = root || document;
        cache[ref.key] = state;
        root.querySelectorAll('.vm-tag[data-vm-key="' + ref.key + '"]').forEach(function(el) {
            el.classList.remove('vm-running', 'vm-stopped', 'vm-unknown', 'vm-error');
            el.classList.add('vm-' + state.status);
            el.title = state.label;
            var label = el.querySelector('.vm-status');
            if (label) label.textContent = state.label;
        });
        return state;
    }

    async function refreshStatuses(clusters, cache, root, request) {
        root = root || document;
        request = request || fetch;
        var refs = Object.create(null);
        Object.values(clusters || {}).forEach(function(cluster) {
            Object.values((cluster && cluster.vms) || {}).forEach(function(vm) {
                try {
                    var ref = identity(vm);
                    if (root.querySelectorAll('.vm-tag[data-vm-key="' + ref.key + '"]').length) {
                        refs[ref.key] = ref;
                    }
                } catch (error) {
                    // renderTag displays the identity error and omits controls for invalid VMs.
                }
            });
        });
        var vmList = Object.values(refs).map(function(ref) {
            return {pve_server_id: ref.pve_server_id, vmid: ref.vmid};
        });
        if (!vmList.length) return;
        try {
            var response = await request('/api/pve/vms/status/batch', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({vms: vmList}),
            });
            var data = await response.json();
            if (!response.ok || !data || data.error || !data.statuses ||
                    typeof data.statuses !== 'object' || Array.isArray(data.statuses)) {
                throw new Error((data && data.error) || '无法获取虚拟机状态');
            }
            Object.keys(refs).forEach(function(expectedKey) {
                var ref = refs[expectedKey];
                if (!Object.prototype.hasOwnProperty.call(data.statuses, expectedKey)) {
                    updateStatus(Object.assign({}, ref, {status: 'unknown'}), cache, root);
                    return;
                }
                try {
                    var result = data.statuses[expectedKey];
                    if (identity(result).key !== expectedKey) {
                        throw new Error('返回的虚拟机身份与请求不一致');
                    }
                    updateStatus(result, cache, root);
                } catch (error) {
                    updateStatus(Object.assign({}, ref, {error: error.message}), cache, root);
                }
            });
        } catch (error) {
            Object.values(refs).forEach(function(ref) {
                updateStatus(Object.assign({}, ref, {error: '无法获取虚拟机状态，请稍后重试'}), cache, root);
            });
        }
    }

    return {
        positiveId: positiveId, key: key, identity: identity, actionUrl: actionUrl,
        clusterProvider: clusterProvider, clusterPayload: clusterPayload, shutdownIdentity: shutdownIdentity,
        statusInfo: statusInfo, renderTag: renderTag, updateStatus: updateStatus, refreshStatuses: refreshStatuses,
    };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = VMIdentity;
