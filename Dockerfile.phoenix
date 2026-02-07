FROM nginx:1.27-alpine

# Copy the full folder so any local assets referenced by the HTML remain available.
COPY . /usr/share/nginx/html/

# Serve the requested page at "/" for convenient local viewing.
RUN cp "/usr/share/nginx/html/Phoenix, AZ (1).html" /usr/share/nginx/html/index.html

EXPOSE 80
