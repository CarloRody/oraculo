(function() {
    var key = localStorage.getItem('oraculo_api_key');
    if (!key) return; // sem chave de cliente = admin, acesso total (comportamento de sempre)

    var page = location.pathname.split('/').pop() || 'index.html';
    if (page === 'index.html' || page === '') return; // portal sempre abre; os cards é que filtram
    // admin.html pode ser restringida como qualquer outra página — se isso
    // te trancar pra fora, é só limpar a chave salva neste navegador
    // (botão "Sair" no index.html, ou localStorage.removeItem('oraculo_api_key')
    // no console) pra voltar a ser tratado como admin, acesso total.

    // Acesso é por cliente (não mais uma lista global igual pra todo mundo) —
    // manda a chave pra API resolver as páginas liberadas DESSE cliente.
    fetch(location.protocol + '//' + location.host + '/api/allowed-pages', { headers: { 'X-Oraculo-Key': key } })
        .then(function(r) { return r.json(); })
        .then(function(data) {
            // active=false = conta desativada na mão pelo admin — mensagem
            // diferente do "sem acesso a esta página" (não é sobre permissão
            // granular, é a conta inteira desligada).
            if (data.active === false) {
                alert('Sua conta está desativada. Entre em contato com o administrador.');
                location.href = 'index.html';
                return;
            }
            // restricted=false = esse cliente ainda não tem nenhuma restrição
            // configurada no admin — acesso total, mesma filosofia de "sem
            // configuração explícita não bloqueia" do resto do sistema.
            if (!data.restricted) return;
            var allowed = data.pages || [];
            if (allowed.indexOf(page) === -1) {
                alert('Você não tem acesso a esta página.');
                location.href = 'index.html';
            }
        })
        .catch(function() { /* falha ao checar não bloqueia — mesma filosofia de falha aberta do resto do sistema */ });
})();
