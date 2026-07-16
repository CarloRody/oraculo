# Páginas Públicas

Serviço: `ai_oraculo_saas` (Flask, porta 5001). Arquivos: `ai_oraculo_saas/frontend/vendas.html` e `ai_oraculo_saas/frontend/ajuda.html`.

## Visão geral

As únicas duas páginas do sistema pensadas pra público externo, sem chave de acesso — servidas pela mesma rota genérica que serve qualquer `.html` de `frontend/` (`GET /<filename>`), sem incluir `access-guard.js` (que é o que restringe as outras páginas por cliente).

## `vendas.html` — vitrine comercial

Landing page de vendas: apresenta o produto, 3 planos ilustrativos (Essencial/Profissional/Business) e todo call-to-action leva pra conversa no WhatsApp pessoal do responsável — sem checkout automático, porque essa parte do sistema não existe (venda é consultiva, cadastro é manual). Link do WhatsApp é montado em JS: `https://wa.me/<número>?text=<mensagem>`, com uma mensagem diferente por seção/plano pra saber de onde veio o contato.

## `ajuda.html` — Central de Ajuda

Guia detalhado pro cliente (novo ou existente): como funciona o cadastro (consultivo, via WhatsApp), como fazer login (colar a chave), o que dá pra fazer no chat e em `meu-portal.html`, como o saldo pré-pago funciona, como recarregar (também manual, via WhatsApp) e um FAQ. Mesmo padrão de link de WhatsApp da `vendas.html`. `vendas.html` tem um link de rodapé pra essa página.

## Conexão com o monitoramento (RAG)

Diferente da documentação técnica em `docs/`, o conteúdo de `ajuda.html` é pensado pra também alimentar a IA — cadastrando-a como uma URL monitorada (`docs/02-monitoramento-automatico.md`) numa área dedicada (ex: "Central de Ajuda"), a IA passa a poder responder perguntas de cliente sobre cadastro/saldo/recarga usando o próprio texto dessa página como fonte, e qualquer atualização futura da página é automaticamente reindexada no próximo rescan — sem trabalho manual extra.

Esse cadastro (criar a área + registrar a URL no monitor + rodar o primeiro scan) ainda não foi feito em produção — ficou pendente de confirmação (é uma mudança de estado que o classificador de permissões pediu confirmação explícita antes de executar).
