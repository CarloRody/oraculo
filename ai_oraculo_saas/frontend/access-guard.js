(async function() {
    // Chegou com ?k= = link cross-origin passando a chave do cliente (Área
    // do Cliente, Monitor Agent, Backup Manager) — salva nesta origem e
    // limpa da URL, mesmo padrão já usado por index.html.
    var params = new URLSearchParams(location.search);
    var urlKey = params.get('k');
    if (urlKey) {
        localStorage.setItem('oraculo_api_key', urlKey);
        params.delete('k');
        var qs = params.toString();
        history.replaceState({}, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
    }

    var page = location.pathname.split('/').pop() || 'index.html';
    if (page === 'index.html' || page === '') return; // portal cuida do próprio gate de login

    var key = localStorage.getItem('oraculo_api_key');
    var origin = location.protocol + '//' + location.host;

    function blockPage(message) {
        alert(message || 'Você precisa entrar com uma chave de acesso pra ver esta página.');
        location.href = 'index.html';
    }

    if (!key) {
        blockPage();
        return;
    }

    try {
        // Chave de admin de verdade (admin_api_key configurada em
        // config.yaml) = acesso total, sem restrição de página nenhuma.
        var adminRes = await fetch(origin + '/admin/whoami', { headers: { 'X-Oraculo-Key': key } });
        if (adminRes.ok) return;

        // Não é admin — tenta como cliente (chave de cliente, restrita por
        // config de "páginas liberadas" desse cliente específico).
        var pagesRes = await fetch(origin + '/api/allowed-pages', { headers: { 'X-Oraculo-Key': key } });
        if (!pagesRes.ok) {
            localStorage.removeItem('oraculo_api_key');
            blockPage('Chave de acesso inválida ou expirada.');
            return;
        }
        var data = await pagesRes.json();
        if (data.active === false) {
            blockPage('Sua conta está desativada. Entre em contato com o administrador.');
            return;
        }
        if (!data.restricted) return; // sem restrição configurada pra esse cliente = acesso total
        var allowed = data.pages || [];
        if (allowed.indexOf(page) === -1) {
            blockPage('Você não tem acesso a esta página.');
        }
    } catch (err) {
        // Falha de rede ao validar — filosofia de "falha fechada" pra rotas
        // administrativas (diferente do resto do sistema, que falha aberto
        // em checagens não-críticas): sem confirmar a chave, bloqueia.
        blockPage('Não foi possível verificar sua chave de acesso agora.');
    }
})();
